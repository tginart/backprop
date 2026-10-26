/**
 * Purpose: Verify the real local CLI, SDK, browser, and SQLite API together.
 * Input: An existing Python environment and an existing Playwright browser.
 * Output: Assertions for live metrics, rich data, persistence, and stop controls.
 * Note: Local uses real HTTP. Optional Foundry mode uses real data with a test authentication proxy.
 */
import assert from 'node:assert/strict';
import {spawn, spawnSync} from 'node:child_process';
import {mkdtemp, readFile, writeFile, rm} from 'node:fs/promises';
import {randomUUID} from 'node:crypto';
import {configureFoundryBrowser} from './browser_foundry_proxy.mjs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {fileURLToPath} from 'node:url';
import {createInterface} from 'node:readline';

const ROOT = fileURLToPath(new URL('../', import.meta.url));
const PYTHON = process.env.BACKPROP_PYTHON || 'python3';
const LIVE_FOUNDRY = Boolean(process.env.BACKPROP_TEST_FOUNDRY_CONFIG);
const LIVE_GCP = Boolean(process.env.BACKPROP_TEST_GCP_CONFIG);
const LIVE = LIVE_FOUNDRY || LIVE_GCP;
const project = LIVE ? `backprop-browser-verification-${randomUUID().slice(0, 12)}` : 'local-smoke';
const entity = LIVE ? 'verification' : 'local';
const {chromium} = await import(process.env.PLAYWRIGHT_MODULE || 'playwright');
const directory = await mkdtemp(join(tmpdir(), 'backprop-local-browser-'));
const env = {...process.env, PYTHONPATH: process.env.BACKPROP_TEST_PACKAGE_PATH || join(ROOT, 'sdk')};
const server = spawn(PYTHON, ['-B', '-m', 'backprop', 'serve',
    join(directory, 'data'), '--port', '0'], {cwd: directory, env});
let trainer, browser;
const errors = [], network = [];
let diagnostics = '';
server.stderr.on('data', chunk => { diagnostics += chunk; });

/**
 * Purpose: Await one process marker without a fixed startup delay.
 * Input: A child process and a predicate for its output lines.
 * Output: The first matching line or an explicit timeout failure.
 */
function waitLine(child, predicate, timeoutMs = LIVE ? 180000 : 20000) {
    return new Promise((resolve, reject) => {
        const lines = createInterface({input: child.stdout});
        const timer = setTimeout(() => finish(new Error(`Process marker timeout: ${diagnostics}`)), timeoutMs);
        const exit = code => finish(new Error(`Process exited ${code}: ${diagnostics}`));
        const finish = (error, line) => {
            clearTimeout(timer);
            lines.close();
            child.off('exit', exit);
            if (error) reject(error); else resolve(line);
        };
        child.once('exit', exit);
        lines.on('line', line => { if (predicate(line)) finish(null, line); });
    });
}

try {
    const startup = await waitLine(server, line => line.startsWith('Backprop dashboard: '));
    let origin = startup.slice('Backprop dashboard: '.length).replace(/\/$/, '');
    let connectionPath = join(directory, 'data/connection.json');
    let liveConfig, liveToken;
    if (LIVE_GCP) {
        connectionPath = process.env.BACKPROP_TEST_GCP_CONFIG;
        liveConfig = JSON.parse(await readFile(connectionPath, 'utf8'));
        origin = liveConfig.host;
        liveToken = process.env.BACKPROP_GCP_ACCESS_TOKEN;
        assert(liveToken, 'Set BACKPROP_GCP_ACCESS_TOKEN for browser acceptance');
    }
    if (LIVE_FOUNDRY) {
        liveConfig = JSON.parse(await readFile(process.env.BACKPROP_TEST_FOUNDRY_CONFIG, 'utf8'));
        liveConfig = {...liveConfig, backend: 'foundry', client_id: 'browser-verification-client', app_url: `${origin}/`};
        connectionPath = join(directory, 'foundry-connection.json');
        await writeFile(connectionPath, JSON.stringify(liveConfig));
        liveToken = process.env.BACKPROP_TOKEN || process.env.FOUNDRY_TOKEN;
        if (!liveToken && process.env.BACKPROP_TEST_DEPLOYMENT_CREDENTIALS === '1') {
            const secret = spawnSync('gcloud', ['secrets', 'versions', 'access', 'latest',
                `--secret=${process.env.BACKPROP_TEST_GCLOUD_SECRET || 'FOUNDRY_TOKEN'}`,
                ...(process.env.BACKPROP_TEST_GCLOUD_PROJECT ? [`--project=${process.env.BACKPROP_TEST_GCLOUD_PROJECT}`] : [])], {encoding: 'utf8', timeout: 45000});
            assert.equal(secret.status, 0, 'Foundry deployment credentials are unavailable');
            liveToken = secret.stdout.trim();
        }
        assert(liveToken, 'Provide Foundry credentials for the real browser data test');
    }
    trainer = spawn(PYTHON, ['-B', join(ROOT, 'tests/local_backend_trainer.py'),
        join(directory, 'run')], {cwd: directory, env: {...env,
            BACKPROP_CONFIG: connectionPath,
            BACKPROP_TEST_PROJECT: project, BACKPROP_TEST_ENTITY: entity,
            BACKPROP_TEST_CAPTURE_OFFSETS: LIVE_FOUNDRY ? '1' : '0',
            BACKPROP_TEST_RUN_ID: LIVE ? `browser-${randomUUID()}` : 'browser-smoke',
            ...(liveToken ? {BACKPROP_TOKEN: liveToken} : {})}});
    trainer.stderr.on('data', chunk => { diagnostics += chunk; });
    const readyLine = await waitLine(trainer, line => line.includes('"ready": true'));
    const initialOffsets = JSON.parse(readyLine.slice(readyLine.indexOf('{'))).initial_offsets;
    browser = await chromium.launch({headless: true,
        executablePath: process.env.CHROMIUM_EXECUTABLE});
    const context = await browser.newContext({viewport: {width: 1440, height: 1000}});
    if (LIVE_GCP) await context.addInitScript(({id, token}) => {
        sessionStorage.setItem(`backprop.google.session.${id}`, JSON.stringify({
            accessToken: token, expiresAt: Date.now() + 3000000,
            identity: {userId: 'verification', displayName: 'Google verification'},
        }));
    }, {id: liveConfig.gcp_id, token: liveToken});
    if (LIVE_FOUNDRY) await configureFoundryBrowser(context, {origin, config: liveConfig,
        token: liveToken, project, initialOffsets});
    const page = await context.newPage();
    page.setDefaultTimeout(LIVE ? 180000 : 30000);
    page.on('pageerror', error => errors.push(error.message));
    page.on('request', req => network.push({url: req.url(), headers: req.headers()}));
    await page.addInitScript(() => {
        const interval = window.setInterval;
        window.setInterval = (fn, ms, ...args) => {
            if (ms === 30000) window.localPoll = () => fn(...args);
            return interval(fn, ms, ...args);
        };
    });
    await page.goto(`${origin}/?project=${encodeURIComponent(`${entity}/${project}`)}`);
    const chart = page.locator('#app [data-metric="train/loss"]');
    await chart.waitFor();
    await page.locator('[data-plotly-ready="true"]').waitFor();
    assert(await page.getByText('local table row', {exact: true}).count());
    assert(await page.getByText(LIVE_GCP ? 'Connected to Google Cloud' : LIVE_FOUNDRY ? 'Connected to Foundry' : 'Connected locally', {exact: true}).count());
    await page.locator('[data-view="setup"]').click();
    const onboarding = await page.locator('.onboarding').textContent();
    assert.equal(onboarding.includes('FOUNDRY_TOKEN'), LIVE_FOUNDRY);
    if (!LIVE) assert(onboarding.includes('/path/to/backprop_foundry-*.whl'));
    const connectionDownload = page.waitForEvent('download');
    await page.locator('[data-action="download-config"]').click();
    const connectionFile = await connectionDownload;
    const connection = JSON.parse(await readFile(await connectionFile.path(), 'utf8'));
    assert.equal(connection.backend, LIVE_GCP ? 'gcp' : LIVE_FOUNDRY ? 'foundry' : 'local');
    assert.equal(connection.host, LIVE ? liveConfig.host : origin);
    if (!LIVE) assert.match(connection.local_id, /^[0-9a-f]{32}$/);
    if (LIVE_GCP) assert.equal(connection.gcp_id, liveConfig.gcp_id);
    await page.locator('[data-view="workspace"]').first().click();
    const slider = chart.locator('[data-panel-smoothing]');
    await slider.evaluate(node => {
        node.value = '0.65';
        node.dispatchEvent(new Event('input', {bubbles: true}));
        node.dispatchEvent(new Event('change', {bubbles: true}));
    });
    await page.reload();
    await chart.waitFor();
    assert.equal(await slider.inputValue(), '0.65');
    if (!LIVE_FOUNDRY) {
        const revisionId = await page.evaluate(async ({project, entity}) => {
            const api = await import('./api.js');
            const {makeWorkspaceEvent} = await import('./workspace-store.mjs');
            const identity = await api.getWorkspaceIdentity();
            const eventId = crypto.randomUUID();
            await api.publishWorkspaceEvent(makeWorkspaceEvent({
                scope: {entity, project, userId: identity.userId, viewId: 'backend-smoke'},
                actorId: identity.userId, name: 'Backend smoke', parents: [],
                eventId, timestamp: new Date().toISOString(),
                snapshot: {version: 1, layout: {sections: []}},
            }));
            return eventId;
        }, {project, entity});
        await page.reload();
        await chart.waitFor();
        const hasSavedWorkspace = await page.evaluate(async eventId => {
            const api = await import('./api.js');
            const workspace = await api.loadWorkspace();
            return workspace.events.some(event => event.event_id === eventId &&
                event.type === 'workspace_snapshot');
        }, revisionId);
        assert(hasSavedWorkspace, 'The local workspace revision did not survive reload');
    }
    const logged = waitLine(trainer, line => line.includes('"logged": true'));
    trainer.stdin.write('log\n');
    await logged;
    await page.evaluate(() => window.localPoll());
    await page.waitForFunction(() =>
        !document.querySelector('[data-action="refresh"]').disabled);
    const metricDeadline = Date.now() + (LIVE ? 60000 : 10000);
    let metricVisible = false;
    while (!metricVisible && Date.now() < metricDeadline) {
        metricVisible = await page.evaluate(async () => {
            const api = await import('./api.js');
            const workspace = await api.loadWorkspace();
            return workspace.events.some(event => event.payload.metrics?.['train/loss'] === 0.0125);
        });
        if (!metricVisible) await new Promise(resolve => setTimeout(resolve, 1000));
    }
    assert(metricVisible, 'The committed live metric did not become visible before the deadline');
    await page.evaluate(() => window.localPoll());
    await page.waitForFunction(() =>
        document.querySelector('[data-metric="train/loss"] .chart-svg')
            ?.textContent.includes('10'));
    const workspace = await page.evaluate(async () => {
        const api = await import('./api.js');
        return api.loadWorkspace();
    });
    assert(workspace.events.some(event => event.payload.metrics?.['train/loss'] === 0.0125));
    assert(workspace.events.some(event => event.type === 'artifact'));
    const download = page.waitForEvent('download');
    await page.locator('[data-run]').filter({hasText: 'Local browser smoke'}).first().click();
    await page.locator('[data-run-tab="artifacts"]').click();
    await page.locator('tr').filter({hasText: 'weights.txt'})
        .locator('[data-download-file]').first().click();
    const artifact = await download;
    assert.equal(await readFile(await artifact.path(), 'utf8'), 'local artifact bytes');
    await page.locator('[data-view="runs"]').first().click();
    await page.locator('[data-inspect-run]').first().click();
    await page.locator('[data-detail-tab="overview"]').click();
    const stopped = waitLine(trainer, line => line.includes('"stopped": true'));
    await page.locator('[data-stop-run]').click();
    await page.getByRole('button', {name: 'Send stop request', exact: true}).click();
    await stopped;
    assert(!network.some(item => item.url.includes('/multipass/')));
    if (!LIVE) assert(network.every(item => !item.headers.authorization));
    if (LIVE_GCP) assert(network.filter(item => item.url.startsWith(origin + '/api/'))
        .every(item => item.headers.authorization === `Bearer ${liveToken}`));
    assert.deepEqual(errors, []);
    if (!LIVE) {
        const foundryConfig = {...connection, backend: 'foundry', host: 'https://foundry.example',
            client_id: 'browser-fixture-client', app_url: `${origin}/`};
        const foundryContext = await browser.newContext();
        const foundryRequests = await configureFoundryBrowser(foundryContext, {origin, config: foundryConfig});
        const foundryPage = await foundryContext.newPage();
        foundryPage.on('pageerror', error => errors.push(error.message));
        await foundryPage.goto(`${origin}/?project=${encodeURIComponent(`${entity}/${project}`)}`);
        await foundryPage.locator('#app [data-metric="train/loss"]').waitFor();
        assert(await foundryPage.getByText('Connected to Foundry', {exact: true}).count());
        await foundryPage.locator('[data-plotly-ready="true"]').waitFor();
        assert(await foundryPage.getByText('local table row', {exact: true}).count());
        assert(foundryRequests.length > 0);
        assert(foundryRequests.every(headers => headers.authorization === 'Bearer browser-fixture-token'));
        assert.deepEqual(errors, []);
        await foundryContext.close();
    }
    console.log(`PASS: installed dashboard, ${LIVE_GCP ? 'real GCP data' : LIVE_FOUNDRY ? 'real Foundry data' : 'real local data and Foundry fixture'}, live metrics, tables, Plotly, artifacts, workspace persistence, and cooperative stop; project=${project}`);
} finally {
    await browser?.close();
    trainer?.kill('SIGTERM');
    server.kill('SIGTERM');
    await Promise.all([trainer, server].filter(Boolean).map(child =>
        child.exitCode !== null || child.signalCode !== null ? undefined :
            new Promise(resolve => child.once('exit', resolve))));
    await rm(directory, {recursive: true, force: true});
}
