/**
 * Architecture: deterministic stress fixture -> intercepted Foundry transport
 * -> exact release HTML/CSP -> real UI actions -> evidence and strict checks.
 *
 * Main caller: node tests/ui_workflow_reference_qa.mjs.
 * UI_WORKFLOW_SMOKE_ONLY=1 verifies transport without claiming workflow parity.
 * UI_WORKFLOW_SAVE_TRANSPORT_ONLY=1 checks real workspace publishing/readback.
 * UI_WORKFLOW_ADVANCED_SHARED_VIEWS=1 opts into the preserved saved-view journey.
 * UI_WORKFLOW_ADVANCED_PANELS=1 opts into custom panel/layout tests. These gates
 * require CUSTOM_WORKSPACES=true, which is outside the current default release.
 * Default checks cover automatic panels, run exploration and project artifacts.
 * PLAYWRIGHT_MODULE, CHROMIUM_EXECUTABLE, PYTHON, and UI_WORKFLOW_OUTPUT can
 * select existing local runtimes and an evidence directory. Nothing is installed.
 *
 * Why this exists: the reference contract must survive independently of the
 * implementation's own tests. Row selection and chart visibility are separate
 * actions. A green transport smoke test is not a green reference walkthrough.
 *
 * Evidence rules: a checkpoint is marked verified only after its assertions
 * pass. Screenshots contain synthetic data. W&B observations below retain their
 * limits; shared saves require the gated journey and local storage is not accepted
 * as evidence of a shared save. No user credentials or experiments are accessed.
 */
import assert from 'node:assert/strict';
import {createServer} from 'node:http';
import {spawnSync} from 'node:child_process';
import {readFile, mkdir, writeFile, rm} from 'node:fs/promises';
import {fileURLToPath} from 'node:url';
import {join, extname, resolve} from 'node:path';
import {makeWorkspaceEvent, replayWorkspaces, workspaceKey} from '../web/workspace-store.mjs';
import {CUSTOM_WORKSPACES} from '../web/experience.mjs';
import {
  createWorkflowReferenceFixture, workflowFoundryRecords, workflowFoundryCsv,
  WORKFLOW_RUN_COUNT, WORKFLOW_SECTIONS, WORKFLOW_METRICS,
} from './fixtures/workflow-reference.mjs';

export const workflowReferenceContract = [
  {
    id:'first-release-scope',evidence:'explicit-user-product-scope',
    behavior:'The first release automatically generates standard scalar and rich panels. It excludes custom panels, layout editing and named/shared workspace controls.',
    requirement:'Default exploration keeps an explicit browser-local preference scope, reads no current-user workspace identity and publishes no workspace snapshots.',
    limit:'Preserved advanced transport and custom UI tests are opt-in evidence for a future layer and cannot count toward this default checkpoint.',
  },
  {
    id: 'workspace-identity', evidence: 'observed',
    source: 'W&B live reference, 2026-10-07; workspace, expanded table, single-run Charts, and exit',
    behavior: 'The same named workspace header remains visible across equivalent analysis views.',
    persistence: 'Navigation and exit observed; reload of a modified personal workspace remains unverified.',
  },
  {
    id: 'selection-versus-visibility', evidence: 'observed-with-explicit-test-requirement',
    source: 'W&B disposable reference view, 2026-10-07',
    behavior: 'Hiding changes Hide run to Show run. Selecting a hidden row retains its hidden eye and exposes 1 selected plus row actions.',
    requirement: 'Checkbox selection must leave plotted series unchanged; this is the independent workflow contract, while the initial reference capture directly proves eye independence.',
    supportedScope: 'This journey checks the selected-actions group. W&B Delete and Move project are outside the supported SDK-management scope; implemented Show/Hide/Compare actions require a separate action walkthrough.',
  },
  {
    id: 'sidebar-table-state', evidence: 'observed-and-required',
    source: 'W&B 2026-10-07 audit and disposable saved-view expansion',
    behavior: 'Expand full table and collapse runs to sidebar are inverse controls. A hidden eye and named workspace survive expansion.',
    requirement: 'Search, filters, pagination, visibility, and row selection must stay coherent across equivalent views.',
  },
  {
    id: 'single-run-return', evidence: 'observed',
    source: 'W&B 2026-10-07 audit, Charts and Exit single-run mode',
    behavior: 'Run Charts has no project runs sidebar. Charts, Overview, Logs, Files, and Artifacts are discoverable. Exit restores the prior workspace.',
    limit: 'The initial read-only audit did not inspect the other tabs contents.',
  },
  {
    id: 'default-chart-run-limit', evidence: 'observed',
    source: 'W&B live settings inspection, 2026-10-07',
    behavior: 'The default chart run limit is ten. The visibility walkthrough filters to six runs with data before comparing plotted series.',
  },
  {
    id: 'shared-saved-view', evidence: 'observed-with-gated-ui-tests',
    source: 'W&B disposable reference view, 2026-10-07',
    behavior: 'Edits show Changes are not auto-saved and enable Undo and Save view. The save confirmation describes teammate sharing. Save yields Saved just now and disabled Save.',
    limit: 'The gated shared-view journey verifies acknowledged writes and fresh-context recovery. Other modes do not verify this gate. Local storage cannot satisfy this behavior.',
  },
  {
    id: 'saved-view-undo', evidence: 'observed-with-gated-ui-tests',
    source: 'W&B live disposable view inspection, 2026-10-07',
    behavior: 'After re-showing a previously hidden run, Undo restores its hidden eye and the clean saved status, then enables Redo.',
    limit: 'The observation establishes this one inverse visibility edit; broader undo/redo ordering remains unverified.',
  },
  {
    id: 'personal-autosave', evidence: 'required-with-gated-ui-tests',
    source: 'User goal requires explicit persistence scope and reload recovery; initial W&B Saved header observation',
    behavior: 'The gated journey checks acknowledged personal autosave and recovery without local browser preferences.',
    limit: 'The initial W&B inspection did not mutate a personal workspace, so this check proves Backprop persistence and does not by itself prove that reference mutation.',
  },
  {
    id: 'panel-bank-and-setting-scopes', evidence: 'observed-controls-with-required-mutation-tests',
    source: 'W&B 2026-10-07 audit, panel and section menus; user supported workflow goal',
    behavior: 'Add panels, Duplicate panel, per-panel editing, workspace settings and section settings are separate discoverable actions.',
    requirement: 'The gated panel journey verifies recorded rich sources, independent duplicates, transactional editing and workspace/section/panel precedence.',
    limit: 'The read-only reference establishes control placement. Exact mutation, reset, inherited smoothing and persistence semantics were not observed there.',
  },
  {
    id: 'run-comparer-and-project-artifacts', evidence: 'required-supported-workflows',
    source: 'User logging and inspection goal; W&B Artifacts rail observed',
    behavior: 'The gated journeys inspect recorded config/summary comparison and immutable artifacts with versions, metadata, usage, files and lineage.',
    limit: 'These checks establish supported Backprop behavior. The reference audit did not inspect comparer mutation or artifact tab content, so no exact reference equivalence is claimed.',
  },
  {
    id: 'unverified-reference-behaviors', evidence: 'unverified',
    behavior: 'Personal autosave mutations, exact hover timing, broader undo/redo semantics, reset scope, column-pin effects, and section/panel mutation persistence.',
    limit: 'No parity claim or reference assertion is made for these behaviors yet.',
  },
];

const root = fileURLToPath(new URL('../', import.meta.url));
const output = resolve(process.env.UI_WORKFLOW_OUTPUT || join(root, 'test-results/workflow-reference'));
const fixture = createWorkflowReferenceFixture();
const initialRecords = workflowFoundryRecords(fixture.events);
const host = 'https://workflow-reference-foundry.invalid';
const token = 'workflow-reference-fixture-only-token';
const fixtureUser = {id: 'fixture-user', username: 'Fixture user'};
const config = {
  host, client_id: 'workflow-reference-client', branch: 'master',
  stream_dataset_rid: 'ri.foundry.main.dataset.workflow-reference-stream',
  media_dataset_rid: fixture.manifest.mediaDatasetRid,
};
const smokeOnly = process.env.UI_WORKFLOW_SMOKE_ONLY === '1';
const saveTransportOnly = process.env.UI_WORKFLOW_SAVE_TRANSPORT_ONLY === '1';
const sharedViews = process.env.UI_WORKFLOW_ADVANCED_SHARED_VIEWS === '1';
const panels = process.env.UI_WORKFLOW_ADVANCED_PANELS === '1';
const extraJourneys = [sharedViews && 'advanced-shared-views', panels && 'advanced-panels'].filter(Boolean);
const visibilityMetric = 'train/accuracy';
const evidence = {
  fixture: {project: fixture.manifest.projectId, runs: WORKFLOW_RUN_COUNT, scalarMetrics: 96, sections: 8, events: fixture.events.length},
  mode: saveTransportOnly ? 'workspace-save-transport' : smokeOnly ? 'transport-smoke' : extraJourneys.length ? extraJourneys.join('-and-') : 'default-reference-workflow', referenceContract: workflowReferenceContract,
  verified: [], failed: null, complete: false,
};
const requests = [], pageErrors = [], transportErrors = [];
let currentStage = 'initialize';

/** Keep acknowledged snapshots in the same mock archive and live stream.
 * Main callers: every fixture browser context and workspace save assertions.
 * Why this exists: a fresh browser must recover server state without inheriting
 * localStorage. Failed writes never enter reads; an uncertain acknowledgment can
 * commit bytes before the connection ends. Event IDs make retries idempotent.
 */
function createDurableWorkspaceTransport() {
  const records = initialRecords.map(record => structuredClone(record));
  const attempts = [], listeners = new Set();
  let failure = {kind: 'none', remaining: 0};
  let archive = '', archiveSize = -1;
  const snapshots = () => records.filter(record => record.event_type === 'workspace_snapshot');
  const event = record => ({...record, type: record.event_type, payload: JSON.parse(record.payload)});
  const scopeMatches = (write, scope) => !scope || workspaceKey(write.event.payload.scope) === workspaceKey(scope);
  function notify(write) { for (const listener of [...listeners]) listener(write); }
  function validateRecord(record) {
    assert.equal(record.event_type, 'workspace_snapshot', 'The workspace fixture accepts display snapshots only');
    assert.equal(record.project, fixture.manifest.project);
    assert.equal(record.entity, fixture.manifest.entity);
    const decoded = event(record);
    assert.equal(decoded.payload.actorId, fixtureUser.id, 'The save editor is the authenticated fixture user');
    replayWorkspaces([...snapshots(), record]);
    return decoded;
  }
  function commit(record) {
    const prior = records.find(value => value.event_id === record.event_id);
    if (prior) {
      assert.deepEqual(record, prior, 'An idempotent retry must preserve the exact acknowledged bytes');
      return true;
    }
    records.push(structuredClone(record));
    return false;
  }
  async function publish(route) {
    assert.equal(route.request().method(), 'POST');
    const body = route.request().postDataJSON();
    assert.equal(body.records?.length, 1, 'Each snapshot is one atomic stream record');
    if (config.stream_view_rid) assert.equal(body.viewRid, config.stream_view_rid);
    const record = body.records[0], decoded = validateRecord(record);
    const kind = failure.remaining > 0 ? failure.kind : 'none';
    if (failure.remaining > 0) failure.remaining--;
    const write = {record: structuredClone(record), event: decoded, failure: kind, committed: false, acknowledged: false, duplicate: false};
    attempts.push(write);
    if (kind === 'deny' || kind === 'unavailable') {
      notify(write);
      return route.fulfill({status: kind === 'deny' ? 403 : 503, body: 'Requested fixture workspace write failure'});
    }
    if (kind === 'network') {
      notify(write);
      return route.abort('failed');
    }
    write.duplicate = commit(record); write.committed = true;
    if (kind === 'uncertain') {
      notify(write);
      return route.abort('failed');
    }
    await route.fulfill({status: 204});
    write.acknowledged = true; notify(write);
  }
  function waitForWrite({after = 0, scope, predicate = () => true, acknowledged = true, timeout = 10000} = {}) {
    const accepts = write => scopeMatches(write, scope) && (!acknowledged || write.acknowledged) && predicate(write.event, write);
    const existing = attempts.slice(after).find(accepts);
    if (existing) return Promise.resolve(structuredClone(existing));
    return new Promise((resolveWrite, rejectWrite) => {
      const timer = setTimeout(() => {listeners.delete(listener); rejectWrite(new Error('The requested workspace write was not observed.'));}, timeout);
      const listener = write => {
        if (attempts.indexOf(write) < after || !accepts(write)) return;
        clearTimeout(timer); listeners.delete(listener); resolveWrite(structuredClone(write));
      };
      listeners.add(listener);
    });
  }
  function assertNoWrites({after = attempts.length, scope, duration = 1000} = {}) {
    assert(!attempts.slice(after).some(write => scopeMatches(write, scope)), 'An explicit-save view must not publish by itself');
    return new Promise((resolveQuiet, rejectQuiet) => {
      const timer = setTimeout(() => {listeners.delete(listener); resolveQuiet();}, duration);
      const listener = write => {
        if (!scopeMatches(write, scope)) return;
        clearTimeout(timer); listeners.delete(listener); rejectQuiet(new Error('An explicit-save view published without a Save action.'));
      };
      listeners.add(listener);
    });
  }
  return {
    publish, waitForWrite, assertNoWrites,
    recordCount: () => records.length,
    records: () => records.map(record => structuredClone(record)),
    workspaceEvents: () => snapshots().map(event),
    writes: () => attempts.map(write => structuredClone(write)),
    latest: scope => replayWorkspaces(snapshots()).get(workspaceKey(scope)),
    archive: () => {if (archiveSize !== records.length) {archive = workflowFoundryCsv(records); archiveSize = records.length;} return archive;},
    live: url => {
      assert.equal(url.searchParams.get('partitionId'), '0');
      const start = Number(url.searchParams.get('startOffset') || 0), limit = Number(url.searchParams.get('limit'));
      assert(Number.isSafeInteger(start) && start >= 0);
      assert(Number.isSafeInteger(limit) && limit > 0);
      return records.slice(start, start + limit).map((value, index) => ({value, offset: String(start + index)}));
    },
    setFailure: (kind = 'none', count = 1) => {
      assert(['none', 'deny', 'unavailable', 'network', 'uncertain'].includes(kind));
      assert(Number.isInteger(count) && count >= 0);
      failure = {kind, remaining: kind === 'none' ? 0 : count};
    },
  };
}
const durable = createDurableWorkspaceTransport();

// Use exactly the policy that deploy.py adds to the release document. The
// subprocess receives fixture configuration and public HTML, never a token.
const policy = spawnSync(process.env.PYTHON || 'python3', ['-B', '-c',
  'import sys; sys.path.insert(0, sys.argv[1]); from deploy import apply_content_security_policy; sys.stdout.buffer.write(apply_content_security_policy(sys.stdin.buffer.read(), {"host": sys.argv[2]}))',
  join(root, 'deploy'), host], {input: await readFile(join(root, 'web/index.html')), encoding: 'utf8'});
assert.equal(policy.status, 0, `Release CSP generation failed: ${policy.stderr}`);
assert(policy.stdout.includes('Content-Security-Policy'), 'The fixture must exercise release CSP');
const webRoot = resolve(root, 'web');
const server = createServer(async (request, response) => {
  try {
    const pathname = decodeURIComponent(new URL(request.url, 'http://localhost').pathname);
    const path = resolve(webRoot, '.' + (pathname === '/' ? '/index.html' : pathname));
    if (!path.startsWith(webRoot + '/')) throw new Error('Invalid fixture asset path');
    const body = pathname === '/__workflow_transport.html' ? policy.stdout.replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi, '')
      : path === join(webRoot, 'index.html') ? policy.stdout : await readFile(path);
    response.writeHead(200, {'Content-Type': ({'.html': 'text/html', '.js': 'text/javascript', '.mjs': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml', '.json': 'application/json'})[extname(path)] || 'application/octet-stream'});
    response.end(body);
  } catch {
    response.writeHead(404); response.end('Fixture asset not found');
  }
});
await new Promise(resolveServer => server.listen(0, '127.0.0.1', resolveServer));
const origin = `http://127.0.0.1:${server.address().port}`;
const playwright = await import(process.env.PLAYWRIGHT_MODULE || 'playwright');
let browser;
let activePage = null;

/** Give each fresh context an independent browser store and the same server.
 * Main callers: the first journey, readback tests, and shared-view tests.
 * Stage boundary: authentication and localStorage belong to the context, while
 * acknowledged archive/live records belong to durable and survive context close.
 */
async function createFixturePage({writeGrant = false, empty = false, viewport = {width: 1600, height: 1050}} = {}) {
  const context = await browser.newContext({viewport, deviceScaleFactor: 1});
  const scopes = ['api:use-streams-read', 'api:use-datasets-read', 'api:use-sql-queries-read', 'api:use-sql-queries-execute', 'offline_access', ...(writeGrant ? ['api:use-streams-write'] : [])];
  await context.addInitScript(({token, scopes}) => {
    if (window.top === window && !sessionStorage.getItem('backprop.oauth.session')) sessionStorage.setItem('backprop.oauth.session', JSON.stringify({accessToken: token, expiresAt: Date.now() + 3600000, scopes}));
  }, {token, scopes});
  await context.route('**/config.json', route => route.fulfill({json: config}));
  await context.route(`${host}/**`, async route => {
    try {
      const url = new URL(route.request().url());
      requests.push({pathname: url.pathname, method: route.request().method()});
      assert.equal(route.request().headers().authorization, `Bearer ${token}`, 'Only the fixture token can reach fixture endpoints');
      if (url.pathname === '/multipass/api/me') return await route.fulfill({json: fixtureUser});
      if (url.pathname.endsWith('/execute')) return await route.fulfill({json: {queryId: 'workflow-reference-query', type: 'succeeded'}});
      if (url.pathname.endsWith('/getResults')) return await route.fulfill({body: empty ? workflowFoundryCsv([]) : durable.archive(), contentType: 'text/csv'});
      if (url.pathname.endsWith('/getEndOffsets')) return await route.fulfill({json: {'0': empty ? '0' : String(durable.recordCount())}});
      if (url.pathname.endsWith('/getRecords')) return await route.fulfill({json: empty ? [] : durable.live(url)});
      if (url.pathname.endsWith('/publishRecords')) {
        assert(writeGrant, 'A read-only fixture context must not submit workspace writes');
        return await durable.publish(route);
      }
      if (url.pathname.includes('/files/')) {
        const path = decodeURIComponent(url.pathname.split('/files/')[1].replace(/\/content$/, ''));
        assert(Object.hasOwn(fixture.files, path), `Unexpected media path: ${path}`);
        return await route.fulfill(fixture.files[path]);
      }
      throw new Error(`Unhandled fixture endpoint: ${route.request().method()} ${url.pathname}`);
    } catch (error) {
      transportErrors.push(error.message);
      await route.fulfill({status: 500, body: error.message});
    }
  });
  const page = await context.newPage();
  activePage = page;
  page.setDefaultTimeout(8000);
  page.on('pageerror', error => pageErrors.push(error.message));
  return {context, page};
}

/** Prepare the real API without booting the application or assuming UI hooks.
 * Main callers: workspace transport verification. The script-free document
 * retains the exact release CSP; only the existing API/auth modules are loaded.
 */
async function initializeFixtureApi(page) {
  await page.goto(origin + '/__workflow_transport.html');
  return page.evaluate(async () => {
    window.workflowFixtureApi = await import('/api.js');
    await window.workflowFixtureApi.initialize();
    const auth = await import('/auth.js');
    return {identity: await window.workflowFixtureApi.getWorkspaceIdentity(), auth: auth.getAuthState()};
  });
}

/** Prove remote readback and failure semantics across real fresh contexts.
 * Main callers: UI_WORKFLOW_SAVE_TRANSPORT_ONLY=1. This is infrastructure
 * evidence, not a substitute for personal/named workspace UI interactions.
 */
async function verifyDurableWorkspaceTransport() {
  currentStage = 'workspace-save-transport';
  const scope = {entity: fixture.manifest.entity, project: fixture.manifest.project, userId: fixtureUser.id, viewId: 'transport-probe'};
  const first = makeWorkspaceEvent({scope, actorId: fixtureUser.id, name: 'Transport proof', parents: [], eventId: 'transport-probe-1', timestamp: '2026-10-07T20:00:00Z', snapshot: {version: 1, hiddenRuns: [fixture.manifest.primaryRunKey], search: 'experiment-025'}});
  const second = makeWorkspaceEvent({scope, actorId: fixtureUser.id, name: 'Transport proof', parents: [first.event_id], eventId: 'transport-probe-2', timestamp: '2026-10-07T20:00:01Z', snapshot: {version: 1, hiddenRuns: [], search: 'experiment-025'}});
  const writer = await createFixturePage({writeGrant:true});
  try {
    const initialized = await initializeFixtureApi(writer.page);
    assert.deepEqual(initialized.identity, {userId: fixtureUser.id, displayName: fixtureUser.username});
    assert.equal(initialized.auth.canSaveWorkspaces, true, 'The fixture token has an explicit stream-write grant');
    assert.deepEqual(await writer.page.evaluate(value => window.workflowFixtureApi.publishWorkspaceEvent(value), first), first);
    assert.equal(durable.latest(scope).revisionId, first.event_id);
    const reader = await createFixturePage({writeGrant: false});
    try {
      const readerAuth = await initializeFixtureApi(reader.page);
      assert.equal(readerAuth.auth.canSaveWorkspaces, false, 'The fresh reader has no write grant');
      const recovered = await reader.page.evaluate(async () => (await window.workflowFixtureApi.loadWorkspace()).events.filter(event => event.type === 'workspace_snapshot'));
      assert.deepEqual(recovered, [first], 'A fresh browser recovers the snapshot through real archive/live reads');
      assert.equal(await reader.page.evaluate(() => localStorage.length), 0, 'Readback does not inherit browser preferences');
    } finally { await reader.context.close(); }

    durable.setFailure('deny');
    const failed = await writer.page.evaluate(async value => {
      try {await window.workflowFixtureApi.publishWorkspaceEvent(value); return {resolved: true};}
      catch (error) {return {resolved: false, status: error.status, message: error.message};}
    }, second);
    assert.equal(failed.resolved, false); assert.equal(failed.status, 403);
    assert(failed.message.includes('denied workspace saving'));
    assert.equal(durable.latest(scope).revisionId, first.event_id, 'Denied bytes never enter saved reads');
    assert.deepEqual(await writer.page.evaluate(value => window.workflowFixtureApi.publishWorkspaceEvent(value), second), second);
    assert.deepEqual(await writer.page.evaluate(value => window.workflowFixtureApi.publishWorkspaceEvent(value), second), second);
    const attempts = durable.writes().filter(write => write.event.event_id === second.event_id);
    assert.equal(attempts.length, 3); assert.equal(attempts[0].acknowledged, false);
    assert.equal(attempts[1].acknowledged, true); assert.equal(attempts[2].duplicate, true);
    assert.deepEqual(attempts[0].record, attempts[1].record, 'Retry preserves denied attempt bytes');
    assert.equal(durable.workspaceEvents().length, 2, 'Idempotent acknowledgment adds no duplicate revision');
    const finalReader = await createFixturePage({writeGrant: false});
    try {
      await initializeFixtureApi(finalReader.page);
      const recovered = await finalReader.page.evaluate(async () => (await window.workflowFixtureApi.loadWorkspace()).events.filter(event => event.type === 'workspace_snapshot'));
      assert.equal(replayWorkspaces(recovered).get(workspaceKey(scope)).revisionId, second.event_id);
      await checkpoint(finalReader.page, 'workflow-workspace-save-transport', 'Current-user identity, explicit writer grant, fresh-browser readback, denied save, exact retry, and idempotent records verified through real API');
    } finally { await finalReader.context.close(); }
  } finally { await writer.context.close(); }
}

/** Save only a checkpoint whose complete assertion set has already passed.
 * Main callers: smoke and reference journey. A failure screenshot is written by
 * the outer catch separately and is never listed as a verified checkpoint.
 */
async function checkpoint(page, id, summary) {
  const screenshot = `${id}.png`;
  await page.evaluate(() => new Promise(resolveFrame => requestAnimationFrame(() => requestAnimationFrame(resolveFrame))));
  await page.screenshot({path: join(output, screenshot), fullPage: false});
  evidence.verified.push({id, summary, screenshot, url: page.url(), viewport: page.viewportSize()});
  console.log(`PASS ${id}: ${summary}`);
}

/** Capture the failing live context before its cleanup can close the page.
 * Main callers: fresh-context checks and the outer runner catch. The first
 * failing state wins, so a cleanup path cannot replace it with another view.
 */
async function recordFailure(error) {
  if (evidence.failed) return;
  evidence.failed = {stage: currentStage, message: error.message, name: error.name};
  if (activePage && !activePage.isClosed()) {
    await activePage.screenshot({path: join(output, 'workflow-failure.png'), fullPage: false});
    evidence.failureScreenshot = 'workflow-failure.png';
  }
}

/** Inspect drawn plot geometry, independent of preference implementation.
 * Main callers: selection and visibility checks. Including strokes and paths
 * prevents a changed curve or recolored series from passing a count-only check.
 */
async function plottedSeries(page) {
  return page.locator(`#app [data-metric=${JSON.stringify(visibilityMetric)}] .chart-svg path[stroke-width="1.8"]`).evaluateAll(paths => paths.map(path => ({
    d: path.getAttribute('d'), stroke: path.getAttribute('stroke'),
    width: path.getAttribute('stroke-width'), dash: path.getAttribute('stroke-dasharray'),
  })));
}
const selectorKey = key => JSON.stringify(key);
const sidebarEye = (page, key) => page.locator(`.run-item [data-run-visibility=${selectorKey(key)}]`);
const tableEye = (page, key) => page.locator(`.runs-table [data-run-visibility=${selectorKey(key)}]`);
const tableSelection = (page, key) => page.locator(`.runs-table [data-run-select=${selectorKey(key)}]`);
const sidebarName = (page, key) => page.locator(`.run-item [data-run=${selectorKey(key)}]`);
const saveViewButton = page => page.locator('.workspace-heading [data-action="save-view"]');

/** Verify the real archive/stream/auth path before enforcing new UI behavior.
 * Main callers: both runner modes. This proves fixture transport and scalar
 * discovery, and deliberately makes no reference-workflow completion claim.
 */
async function verifyTransport(page) {
  currentStage = 'transport';
  await page.goto(`${origin}/?project=${encodeURIComponent(fixture.manifest.projectId)}&view=workspace`);
  await page.locator('#app .chart-svg').first().waitFor({timeout: 30000});
  assert.equal(await page.locator('.header-project').inputValue(), fixture.manifest.projectId);
  assert.equal(await page.locator('.run-panel-title .count').textContent(), String(WORKFLOW_RUN_COUNT));
  assert.equal(await page.locator('.run-item').count(), 20);
  const expectedSections = [...WORKFLOW_SECTIONS, 'samples'];
  assert.equal(await page.locator('[data-section-id]').count(), expectedSections.length, 'Scalar and rich-output sections share the panel bank');
  for (const section of expectedSections) assert.equal(await page.locator(`[data-section-id=${selectorKey(section)}]`).count(), 1);
  assert(await page.getByText('Synthetic demo', {exact: true}).isVisible());
  assert(!(await page.locator('#app').innerText()).includes('Unsupported event type'));
  assert(requests.some(request => request.pathname.endsWith('/getResults')), 'Real archive endpoint must be exercised');
  assert(requests.some(request => request.pathname.endsWith('/getRecords')), 'Real stream endpoint must be exercised');
  assert.equal(transportErrors.length, 0, transportErrors.join('\n'));
  assert.deepEqual(pageErrors, []);
  await checkpoint(page, 'workflow-transport-desktop', '256 synthetic runs and 96 scalar names loaded through release CSP and real app transport');
}

/** Check the explicitly scoped default workspace identity across navigation.
 * Main callers: default reference navigation checkpoints. The first release
 * deliberately excludes custom/named workspaces; its identity is Workspace.
 */
async function workspaceIdentity(page) {
  const heading = page.locator('.workspace-heading[data-workspace-default]');
  assert.equal(await heading.count(), 1, 'Workspace identity header must exist exactly once');
  assert(await heading.isVisible(), 'Workspace identity header must remain visible');
  const name = (await heading.locator('h1').textContent()).trim();
  assert.equal(name,'Workspace');
  assert.equal(new URL(page.url()).searchParams.has('nw'),false,'Default navigation cannot imply a saved named workspace');
  return {name};
}

/** Drive the first complete reference journey without reading localStorage.
 * Main callers: normal runner mode. Search/filter results derive from fixture
 * inputs, while selection and visibility assertions derive from W&B behavior.
 * Shared saving has its own advanced gate and cannot be inferred here.
 */
async function verifyReferenceJourney(page) {
  currentStage = 'workspace-contract';
  const identity = await workspaceIdentity(page);
  assert.equal(await page.locator('.run-item [data-run-select]').count(), 0, 'Collapsed sidebar uses chart visibility, not row-selection checkboxes');
  assert.equal(await page.locator('.run-item [data-run-visibility]').count(), 20);
  assert(await page.getByRole('button', {name: 'Previous runs page', exact: true}).isDisabled());
  await page.getByRole('button', {name: 'Next runs page', exact: true}).click();
  assert((await page.locator('.run-pagination').innerText()).includes('21-40 of 256'));
  await page.getByRole('button', {name: 'Previous runs page', exact: true}).click();
  assert((await page.locator('.run-pagination').innerText()).includes('1-20 of 256'));
  await checkpoint(page, 'workflow-workspace-contract', 'Persistent workspace header, distinct sidebar eye controls, and 256-run pagination');

  currentStage = 'search-filter-expansion';
  await page.locator('[data-state="search"]').fill('experiment-02');
  assert.equal(await page.locator('.run-panel-title .count').textContent(), '256', 'Project total is independent of listed count');
  assert.equal(await page.locator('.run-list-columns .count').textContent(), '56 listed');
  await page.locator('[data-action="run-filters"]').click();
  await page.locator('[data-filter-action="add"]').click();
  await page.locator('[data-filter-part="field"]').selectOption('config:accelerator_count');
  await page.locator('[data-filter-part="op"]').selectOption('missing');
  assert(await page.locator('[data-filter-part="value"]').isDisabled(), 'A missing-value filter takes no operand');
  assert.equal(await page.locator('[data-filter-preview]').textContent(), '29 of 256 runs match');
  await page.locator('[data-filter-action="apply"]').click();
  assert.equal(await page.locator('.run-panel-title .count').textContent(), '256');
  assert.equal(await page.locator('.run-list-columns .count').textContent(), '6 listed');
  assert.equal(await page.locator('.run-item').count(), 6);
  const filteredKeys = await page.locator('.run-item [data-run]').evaluateAll(nodes => nodes.map(node => node.dataset.run));
  const expectedKeys = fixture.events.filter(event => event.type === 'run_start' && event.payload.name.startsWith('experiment-02') && !Object.hasOwn(event.payload.config, 'accelerator_count'))
    .map(event => [event.entity, event.project, event.run_id].join('/')).sort();
  assert.deepEqual(filteredKeys.slice().sort(), expectedKeys);
  await page.locator('[data-action="expand-run-table"]').click();
  assert.equal(new URL(page.url()).searchParams.get('view'), 'runs');
  assert.deepEqual(await workspaceIdentity(page), identity);
  assert.equal(await page.locator('[data-state="search"]').inputValue(), 'experiment-02');
  assert.equal(await page.locator('.runs-table [data-run-select]').count(), 6);
  assert.deepEqual((await page.locator('.runs-table [data-run-select]').evaluateAll(nodes => nodes.map(node => node.dataset.runSelect))).sort(), expectedKeys);
  await checkpoint(page, 'workflow-filtered-full-table', 'Search and missing-value filter retain exactly the same six runs in the full table');
  await page.locator('[data-action="collapse-run-table"]').click();
  assert.equal(new URL(page.url()).searchParams.get('view'), 'workspace');
  assert.deepEqual(await workspaceIdentity(page), identity);
  assert.equal(await page.locator('.run-item').count(), 6);
  assert.equal(await page.locator('[data-state="search"]').inputValue(), 'experiment-02');
  await page.locator('[data-action="run-filters"]').click();
  await page.locator('[data-filter-action="clear"]').click();
  await page.locator('[data-filter-action="apply"]').click();
  assert.equal(await page.locator('.run-list-columns .count').textContent(), '56 listed');
  await page.locator('[data-state="search"]').fill('');
  assert.equal(await page.locator('.run-list-columns .count').textContent(), '256 listed');

  currentStage = 'eye-visibility';
  // Use six known runs, below the reference's default chart limit of ten.
  // The selected scalar is on the first panel page and is logged by all six.
  // Searching is real user setup, so paging cannot make the plot oracle empty.
  await page.locator('[data-state="search"]').fill('experiment-025');
  assert.equal(await page.locator('.run-list-columns .count').textContent(), '6 listed');
  await page.locator('[data-state="metricSearch"]').fill(visibilityMetric);
  assert.equal(await page.locator('#app .chart-card').count(), 1);
  assert(await page.locator(`#app [data-metric=${JSON.stringify(visibilityMetric)}] .chart-svg`).isVisible());
  const visibleEye = page.locator('.run-item [data-run-visibility][aria-pressed="true"]').first();
  assert(await visibleEye.isVisible(), 'The fixture must start with at least one visible run');
  const key = await visibleEye.getAttribute('data-run-visibility');
  const name = await sidebarName(page, key).textContent();
  const beforeHide = await plottedSeries(page);
  assert(beforeHide.length > 1, 'Visibility independence needs at least two plotted runs');
  assert.equal(beforeHide.length, 6, 'All six fixture runs have the chosen metric and fit the reference chart limit');
  assert((await visibleEye.getAttribute('aria-label')).startsWith('Hide run'));
  await visibleEye.click();
  assert.equal(await sidebarEye(page, key).getAttribute('aria-pressed'), 'false');
  assert((await sidebarEye(page, key).getAttribute('aria-label')).startsWith('Show run'));
  const afterHide = await plottedSeries(page);
  assert.equal(afterHide.length, beforeHide.length - 1, `Hiding a run removes exactly its ${visibilityMetric} series`);
  await page.getByRole('button', {name: 'Hide runs sidebar', exact: true}).click();
  assert.equal(await page.locator('.run-panel').count(), 0);
  assert.deepEqual(await plottedSeries(page), afterHide);
  await page.getByRole('button', {name: 'Show runs', exact: true}).click();
  assert.equal(await sidebarEye(page, key).getAttribute('aria-pressed'), 'false');
  assert.deepEqual(await plottedSeries(page), afterHide);
  await checkpoint(page, 'workflow-hidden-run-sidebar', 'Eye hides one plotted run, and hiding/restoring the sidebar preserves that visibility');

  currentStage = 'row-selection-independence';
  await page.locator('[data-action="expand-run-table"]').click();
  assert.deepEqual(await workspaceIdentity(page), identity);
  assert.equal(await page.locator('.runs-table [data-run-select]:checked').count(), 0, 'No rows are selected merely because runs are plotted');
  assert.equal(await tableEye(page, key).getAttribute('aria-pressed'), 'false');
  await tableSelection(page, key).check();
  assert.equal(await page.locator('.runs-table [data-run-select]:checked').count(), 1);
  assert.equal(await tableEye(page, key).getAttribute('aria-pressed'), 'false', 'Selecting a hidden row must not show it in charts');
  assert(await page.getByText('1 selected', {exact: true}).isVisible());
  assert(await page.getByRole('group', {name: 'Selected run actions', exact: true}).isVisible(), 'Selection exposes a separately labeled row-actions group');
  await checkpoint(page, 'workflow-selected-hidden-row', 'Hidden run remains hidden while its independently selected row exposes bulk actions');
  await page.locator('[data-action="collapse-run-table"]').click();
  assert.equal(await sidebarEye(page, key).getAttribute('aria-pressed'), 'false');
  assert.deepEqual(await plottedSeries(page), afterHide, 'Selecting a table row must not change plotted geometry or series colors');

  currentStage = 'single-run-return';
  const returnUrl = page.url();
  await sidebarName(page, key).click();
  assert.equal(new URL(page.url()).searchParams.get('view'), 'run');
  assert.deepEqual(await workspaceIdentity(page), identity);
  assert.equal(await page.locator('.run-panel').count(), 0, 'Single-run Charts omits the project runs sidebar');
  assert.equal(await page.locator('.run-page h1').textContent(), name);
  const runTabs = page.getByRole('navigation', {name: 'Run details', exact: true});
  for (const tab of ['Charts', 'Overview', 'Logs', 'Files', 'Artifacts']) assert(await runTabs.getByRole('button', {name: tab, exact: true}).isVisible(), `Run tab ${tab} is discoverable`);
  currentStage='single-run-source-discovery';
  await page.locator('[data-state="metricSearch"]').fill('');
  const singleRunId=key.slice(fixture.manifest.projectId.length+1);
  const loggedKeys=WORKFLOW_METRICS.filter(metric=>fixture.events.some(event=>event.run_id===singleRunId&&event.type==='history'&&typeof event.payload.metrics[metric]==='number'));
  assert(loggedKeys.length<WORKFLOW_METRICS.length,'The selected run deliberately lacks whole scalar series');
  for(const sectionName of WORKFLOW_SECTIONS){
    const expected=loggedKeys.filter(metric=>metric.startsWith(sectionName+'/')).length;
    const section=page.locator(`[data-section-id=${selectorKey(sectionName)}]`);
    assert((await section.locator('[data-section]').innerText()).includes(`${expected} panels`),'A single-run section discovers only that run\'s recorded keys');
  }
  assert.equal(await page.locator('[data-section-id="samples"]').count(),0,'A run without samples does not inherit rich panels from other runs');
  assert.equal(await page.locator('.run-page-content article.logged-panel').count(),0);
  await checkpoint(page,'workflow-single-run-source-discovery','Single-run discovery excludes scalar series and rich sources recorded only in other runs');
  await page.locator('[data-state="metricSearch"]').fill(visibilityMetric);
  currentStage='single-run-return';
  await checkpoint(page, 'workflow-single-run-charts', 'Run Charts preserves workspace identity and exposes the five reference run destinations');
  await page.getByRole('button', {name: 'Exit single-run view', exact: true}).click();
  assert.equal(page.url(), returnUrl, 'Exiting returns to the exact prior workspace URL');
  assert.deepEqual(await workspaceIdentity(page), identity);
  assert.equal(await sidebarEye(page, key).getAttribute('aria-pressed'), 'false');
  assert.deepEqual(await plottedSeries(page), afterHide);
  await page.locator('[data-action="expand-run-table"]').click();
  assert(await tableSelection(page, key).isChecked(), 'Row selection survives visiting and exiting a run');
  await tableSelection(page, key).uncheck();
  assert.equal(await page.locator('.runs-table [data-run-select]:checked').count(), 0);
  assert.equal(await tableEye(page, key).getAttribute('aria-pressed'), 'false', 'Deselecting a row also preserves visibility');
  await page.locator('[data-action="collapse-run-table"]').click();
  assert.deepEqual(await plottedSeries(page), afterHide, 'Deselecting a table row must not change plotted series');
  await page.locator('[data-action="expand-run-table"]').click();
  await tableEye(page, key).click();
  assert.equal(await tableEye(page, key).getAttribute('aria-pressed'), 'true');
  assert.equal(await page.locator('.runs-table [data-run-select]:checked').count(), 0, 'Showing a run must not select its row');
  await page.locator('[data-action="collapse-run-table"]').click();
  assert.deepEqual(await plottedSeries(page), beforeHide, 'Showing the run restores the original plot independently of row selection');
  await checkpoint(page, 'workflow-returned-workspace', 'Run exit restores workspace state; selecting, deselecting, and showing runs remain independent');

  currentStage = 'narrow-layout';
  await page.setViewportSize({width: 390, height: 844});
  await page.evaluate(() => new Promise(resolveFrame => requestAnimationFrame(() => requestAnimationFrame(resolveFrame))));
  assert.equal(await page.locator('.topbar').count(), 1, 'Narrow layout retains one application header');
  assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), 'Narrow viewport must not cause document overflow');
  assert.deepEqual(await workspaceIdentity(page), identity);
  assert(await page.locator('[data-action="expand-run-table"]').isVisible(), 'Table expansion stays discoverable on a narrow screen');
  await checkpoint(page, 'workflow-workspace-narrow', 'Workspace identity and table expansion remain accessible at 390 pixels');
  await page.setViewportSize({width: 1600, height: 1050});
}

const scopeForView = viewId => ({entity: fixture.manifest.entity, project: fixture.manifest.project, userId: fixtureUser.id, viewId});

/** Wait for an actual UI status without inferring success from browser storage.
 * Main callers: personal autosave, explicit shared saves, and failed-save retry.
 */
async function waitForWorkspaceStatus(page, text) {
  await page.waitForFunction(expected => document.querySelector('[data-workspace-status]')?.textContent.trim() === expected, text, {timeout: 10000});
}

/** Open the application with no inherited local preferences and a real link.
 * Main callers: personal/named reload checks. The fixture identity stays the
 * same across contexts while OAuth grant and browser stores stay independent.
 */
async function openWorkspaceFixture({viewId = 'personal', writeGrant = true} = {}) {
  const fixturePage = await createFixturePage({writeGrant});
  await fixturePage.page.goto(origin + '/__workflow_transport.html');
  assert.equal(await fixturePage.page.evaluate(() => localStorage.length), 0, 'Fresh context starts with no local workspace preferences');
  await fixturePage.page.goto(`${origin}/?project=${encodeURIComponent(fixture.manifest.projectId)}&view=workspace&nw=${encodeURIComponent(viewId)}`);
  await fixturePage.page.locator('.workspace-heading').waitFor({timeout: 30000});
  await fixturePage.page.locator('.run-item').first().waitFor({timeout: 30000});
  assert.equal(await fixturePage.page.locator('[data-view-choice]').inputValue(), viewId);
  assert.equal(new URL(fixturePage.page.url()).searchParams.get('nw'), viewId);
  return fixturePage;
}

/** Verify acknowledged personal and shared state through the user controls.
 * Main callers: UI_WORKFLOW_ADVANCED_SHARED_VIEWS=1 with the custom layer enabled.
 * Why this exists: personal autosave and named explicit saves have different
 * lifecycles. Selection stays transient; hiddenRuns is a display preference.
 * Fresh contexts prove remote recovery, and a denied write must remain Not saved.
 */
async function verifySharedWorkspaceJourney() {
  const personalScope = scopeForView('personal');
  const key = `${fixture.manifest.projectId}/workflow-0255`;
  const writer = await openWorkspaceFixture();
  const page = writer.page;
  try {
    currentStage = 'personal-autosave';
    await page.locator('[data-state="search"]').fill('experiment-025');
    await page.locator('[data-state="metricSearch"]').fill(visibilityMetric);
    const hidden = await page.locator('.run-item [data-run-visibility][aria-pressed="false"]').evaluateAll(nodes => nodes.map(node => node.dataset.runVisibility));
    for (const runKey of hidden) await sidebarEye(page, runKey).click();
    await waitForWorkspaceStatus(page, 'Saved');
    assert.equal(await sidebarEye(page, key).getAttribute('aria-pressed'), 'true');
    const afterBaseline = durable.writes().length;
    await sidebarEye(page, key).click();
    assert.equal(await sidebarEye(page, key).getAttribute('aria-pressed'), 'false');
    const personalWrite = await durable.waitForWrite({after: afterBaseline, scope: personalScope, predicate: event => event.payload.snapshot.hiddenRuns.includes(key)});
    await waitForWorkspaceStatus(page, 'Saved');
    assert.equal(durable.latest(personalScope).revisionId, personalWrite.event.event_id);
    assert.equal(durable.latest(personalScope).snapshot.search, 'experiment-025');
    const personalReader = await openWorkspaceFixture({writeGrant: false});
    try {
      assert.equal(await personalReader.page.locator('[data-state="search"]').inputValue(), 'experiment-025');
      assert.equal(await sidebarEye(personalReader.page, key).getAttribute('aria-pressed'), 'false');
      await waitForWorkspaceStatus(personalReader.page, 'Saved');
      await checkpoint(personalReader.page, 'workflow-personal-autosave-reload', 'An acknowledged personal visibility edit and search recover in a fresh read-only browser context');
    } catch (error) {await recordFailure(error); throw error;}
    finally {await personalReader.context.close(); activePage = page;}

    currentStage = 'named-view-create';
    const beforeNamed = durable.writes().length;
    await page.locator('[data-action="save-view-as"]').click();
    const form = page.locator('[data-save-view-form]');
    await form.locator('input[name="name"]').fill('Workflow shared reference');
    assert((await form.innerText()).includes("This view is available to people with access to this project's data."), 'The named-save dialog must explain project-reader access to the shared view');
    await form.getByRole('button', {name: 'Save view', exact: true}).click();
    const created = await durable.waitForWrite({after: beforeNamed, predicate: event => event.payload.scope.viewId !== 'personal' && event.payload.name === 'Workflow shared reference'});
    const namedScope = created.event.payload.scope, viewId = namedScope.viewId;
    await waitForWorkspaceStatus(page, 'Saved');
    assert.equal(await page.locator('[data-view-choice]').inputValue(), viewId);
    assert.equal(new URL(page.url()).searchParams.get('nw'), viewId);
    assert(await saveViewButton(page).isDisabled());
    assert(!(await page.locator('.workspace-heading').innerText()).includes('On this device'), 'Shared persistence must not retain a local-only label');
    await checkpoint(page, 'workflow-shared-view-created', 'Save as acknowledges a project-shared named snapshot and selects its stable URL identity');

    currentStage = 'named-view-undo-redo-clear';
    const beforeDraft = durable.writes().length;
    await sidebarEye(page, key).click();
    await waitForWorkspaceStatus(page, 'Unsaved changes');
    assert(await saveViewButton(page).isEnabled());
    assert(await page.locator('[data-action="undo-view"]').isEnabled());
    assert(await page.locator('[data-action="clear-view-changes"]').isVisible());
    await durable.assertNoWrites({after: beforeDraft, scope: namedScope});
    await page.locator('[data-action="undo-view"]').click();
    assert.equal(await sidebarEye(page, key).getAttribute('aria-pressed'), 'false');
    await waitForWorkspaceStatus(page, 'Saved');
    assert(await page.locator('[data-action="redo-view"]').isEnabled());
    assert(await saveViewButton(page).isDisabled());
    await page.locator('[data-action="redo-view"]').click();
    assert.equal(await sidebarEye(page, key).getAttribute('aria-pressed'), 'true');
    await waitForWorkspaceStatus(page, 'Unsaved changes');
    await page.locator('[data-action="clear-view-changes"]').click();
    assert.equal(await sidebarEye(page, key).getAttribute('aria-pressed'), 'false');
    await waitForWorkspaceStatus(page, 'Saved');
    await durable.assertNoWrites({after: beforeDraft, scope: namedScope});
    await checkpoint(page, 'workflow-shared-view-history', 'Named edits wait for Save; Undo restores the hidden eye and clean status, Redo replays it, and Clear changes restores the baseline');

    currentStage = 'named-view-save-reload';
    const beforeSave = durable.writes().length;
    await sidebarEye(page, key).click();
    await waitForWorkspaceStatus(page, 'Unsaved changes');
    await saveViewButton(page).click();
    const updated = await durable.waitForWrite({after: beforeSave, scope: namedScope, predicate: event => !event.payload.snapshot.hiddenRuns.includes(key)});
    assert.deepEqual(updated.event.payload.parents, [created.event.event_id], 'The update retains its acknowledged named-view parent');
    await waitForWorkspaceStatus(page, 'Saved');
    const namedReader = await openWorkspaceFixture({viewId, writeGrant: false});
    try {
      assert.equal(await namedReader.page.locator('[data-view-choice] option:checked').textContent(), 'Workflow shared reference');
      assert.equal(await sidebarEye(namedReader.page, key).getAttribute('aria-pressed'), 'true');
      await waitForWorkspaceStatus(namedReader.page, 'Saved');
      await checkpoint(namedReader.page, 'workflow-shared-view-save-reload', 'An explicit named Save updates its acknowledged revision and recovers via its URL in a fresh browser');
    } catch (error) {await recordFailure(error); throw error;}
    finally {await namedReader.context.close(); activePage = page;}

    currentStage = 'named-view-write-failure';
    const beforeFailure = durable.writes().length;
    durable.setFailure('deny');
    await sidebarEye(page, key).click();
    await saveViewButton(page).click();
    const denied = await durable.waitForWrite({after: beforeFailure, scope: namedScope, acknowledged: false, predicate: (_event, write) => write.failure === 'deny'});
    await waitForWorkspaceStatus(page, 'Not saved');
    assert.equal(denied.committed, false); assert.equal(denied.acknowledged, false);
    assert.equal(durable.latest(namedScope).revisionId, updated.event.event_id, 'A failed save retains the acknowledged remote revision');
    assert((await page.locator('#app').innerText()).includes('Foundry denied workspace saving'), 'The failed save remains visible to the user');
    assert(await saveViewButton(page).isEnabled(), 'The failed save can be explicitly retried');
    const beforeRetry = durable.writes().length;
    await saveViewButton(page).click();
    const retried = await durable.waitForWrite({after: beforeRetry, scope: namedScope, predicate: event => event.event_id === denied.event.event_id});
    assert.deepEqual(retried.record, denied.record, 'UI retry preserves the exact unacknowledged snapshot bytes');
    await waitForWorkspaceStatus(page, 'Saved');
    await checkpoint(page, 'workflow-shared-view-failure-retry', 'Denied writes show Not saved and retain remote state; explicit retry acknowledges the same immutable revision');
  } catch (error) {await recordFailure(error); throw error;}
  finally {await writer.context.close();}
}

const panelCard = (page, id) => page.locator(`#app article.chart-card[data-metric=${selectorKey(id)}]`);
const panelForm = page => page.locator('#panel-dialog[open] #panel-form');
const scopeForm = page => page.locator('#inspect-dialog[open] form');

/** Measure ordinary run rows after layout, including long fixture names.
 * Main callers: default sidebar before pinning and after a visible column pin.
 * Why this exists: a zero-column CSS grid can silently make every control a
 * separate tall row even while selection/visibility behavior still passes.
 */
async function assertCompactRunRows(page) {
  const heights=await page.locator('.run-item').evaluateAll(nodes=>nodes.map(node=>node.getBoundingClientRect().height));
  assert(heights.length>0);assert(heights.every(height=>height>0&&height<60),`Desktop run rows must stay compact: ${heights.join(', ')}`);
}

/** Compare only charts at the same placement and viewport.
 * Main callers: panel draft cancellation, duplicate independence and scope tests.
 * Why this exists: previews and expanded views have different geometry. Reading
 * only the workspace card avoids mistaking that difference for a data change.
 */
async function panelSeries(page, id) {
  return panelCard(page, id).locator('.chart-svg path[stroke-width="1.8"]').evaluateAll(paths => paths.map(path => ({d:path.getAttribute('d'),stroke:path.getAttribute('stroke')})));
}

/** Start a named draft so edits remain reviewable until explicitly saved.
 * Main callers: panel and artifact journeys. The remote personal baseline is
 * allowed to settle before copying it; no browser preference becomes evidence.
 */
async function saveAsFixtureView(page, name) {
  await waitForWorkspaceStatus(page, 'Saved');
  const after = durable.writes().length;
  await page.locator('[data-action="save-view-as"]').click();
  const form = page.locator('[data-save-view-form]');
  await form.locator('[name="name"]').fill(name);
  await form.getByRole('button', {name:'Save view',exact:true}).click();
  const write = await durable.waitForWrite({after,predicate:event => event.payload.name === name && event.payload.scope.viewId !== 'personal'});
  await waitForWorkspaceStatus(page, 'Saved');
  return write.event.payload.scope;
}

/** Read the resolved editor values through the same controls a user sees.
 * Main callers: inheritance and panel override checks. Closing this editor is
 * an explicit Cancel, so inspecting defaults cannot commit an override.
 */
async function assertPanelDefaults(page, id, maxRuns, smoothing) {
  await panelCard(page,id).locator('[data-edit-panel]').click();
  const form = panelForm(page);
  await form.getByRole('tab',{name:'Display',exact:true}).click();
  assert.equal(Number(await form.locator('[name="maxRuns"]').inputValue()),maxRuns);
  assert.equal(Number(await form.locator('[name="smoothing"]').inputValue()),smoothing);
  await form.getByRole('button',{name:'Cancel',exact:true}).click();
}

/** Prove typed creation and transactional editing from fixture sources.
 * Main callers: UI_WORKFLOW_ADVANCED_PANELS=1 with the custom layer enabled. UI actions choose real logged keys; chart
 * counts and resolved values independently enforce inheritance. Row checkboxes
 * must never choose Run comparer columns. Only the final named save publishes.
 */
async function verifyPanelJourney() {
  const writer = await openWorkspaceFixture(), page = writer.page;
  let scope;
  try {
    currentStage = 'automatic-rich-panel-bank';
    await page.locator('[data-state="search"]').fill('experiment-000');
    await page.locator('[data-state="metricSearch"]').fill('samples/');
    scope = await saveAsFixtureView(page,'Workflow panel reference');
    const beforeDraft = durable.writes().length;
    const rich = page.locator('#app article.logged-panel');
    assert.equal(await rich.count(),3,'Automatic discovery includes image, table and media gallery panels');
    const autoImage = page.locator('#app article[data-panel-type="image"][data-source="samples/image"]');
    const autoTable = page.locator('#app article[data-panel-type="table"][data-source="samples/predictions"]');
    assert.equal(await autoImage.count(),1); assert.equal(await autoTable.count(),1);
    assert.equal(await autoImage.locator('.logged-panel-entry').count(),8);
    assert.equal(await autoTable.locator('.logged-panel-entry').count(),8);
    await autoImage.locator('.image-viewer').first().waitFor();
    assert.equal(await autoTable.locator('.logged-table').count(),8);
    await autoTable.locator('[data-table-option][data-option="limit"]').first().selectOption('25');
    assert((await autoTable.innerText()).includes('1-25 of 96'));
    assert.equal(await page.locator('#app article[data-panel-type="media"]').count(),1);
    await checkpoint(page,'workflow-automatic-rich-panels','Image, table and gallery panels discover eight recorded outputs in the shared workspace bank');

    currentStage = 'quick-add-rich-panels';
    const autoImageId = await autoImage.getAttribute('data-metric'), autoTableId = await autoTable.getAttribute('data-metric');
    await page.locator('[data-action="add-panels"]').click();
    const library = page.locator('#inspect-dialog[open]');
    assert((await library.innerText()).includes('Quick add from logged keys'));
    await library.getByRole('button',{name:'Add image panel for samples/image',exact:true}).click();
    await library.getByRole('button',{name:'Add table panel for samples/predictions',exact:true}).click();
    await library.locator('[data-library-close]').click();
    const imageIds = await page.locator('#app article[data-panel-type="image"][data-source="samples/image"]').evaluateAll(nodes=>nodes.map(node=>node.dataset.metric));
    const tableIds = await page.locator('#app article[data-panel-type="table"][data-source="samples/predictions"]').evaluateAll(nodes=>nodes.map(node=>node.dataset.metric));
    assert.equal(imageIds.length,2);assert.equal(new Set(imageIds).size,2);
    assert.equal(tableIds.length,2);assert.equal(new Set(tableIds).size,2);
    const addedImageId = imageIds.find(id=>id!==autoImageId), addedTableId = tableIds.find(id=>id!==autoTableId);
    assert.equal(await panelCard(page,addedImageId).locator('.logged-panel-entry').count(),8);
    assert.equal(await panelCard(page,addedTableId).locator('.logged-table').count(),8);
    await page.locator('[data-state="metricSearch"]').fill('evaluation/confidence_distribution');
    assert.equal(await page.locator('#app article[data-panel-type="histogram"]').count(),1);
    assert.equal(await page.locator('#app article[data-panel-type="histogram"] .logged-panel-entry').count(),8);
    await page.locator('[data-state="metricSearch"]').fill('evaluation/class_accuracy');
    assert.equal(await page.locator('#app article[data-panel-type="plot"]').count(),1);
    assert.equal(await page.locator('#app article[data-panel-type="plot"] .logged-panel-entry').count(),8);
    await checkpoint(page,'workflow-quick-add-rich-panels','Quick Add creates independent image/table instances; automatic histogram and plot outputs remain inspectable');

    currentStage = 'duplicate-scalar-panel';
    await page.locator('[data-state="search"]').fill('experiment-025');
    await page.locator('[data-state="metricSearch"]').fill(visibilityMetric);
    for(const key of await page.locator('.run-item [data-run-visibility][aria-pressed="false"]').evaluateAll(nodes=>nodes.map(node=>node.dataset.runVisibility)))await sidebarEye(page,key).click();
    const originalId = visibilityMetric, beforeDuplicate = await panelSeries(page,originalId);
    assert.equal(beforeDuplicate.length,6);
    await panelCard(page,originalId).locator('[data-panel-menu]').click();
    await page.getByRole('menuitem',{name:'Duplicate panel',exact:true}).click();
    const scalarIds = await page.locator('#app article.chart-card[data-metric]').evaluateAll(nodes=>nodes.map(node=>node.dataset.metric));
    assert.equal(scalarIds.length,2);assert.equal(new Set(scalarIds).size,2);
    const copyId = scalarIds.find(id=>id!==originalId);
    assert.deepEqual(await panelSeries(page,originalId),beforeDuplicate,'Duplicating does not alter the source card');
    assert.equal((await panelSeries(page,copyId)).length,6);
    const copyTitle = await panelCard(page,copyId).locator('.chart-title').textContent();
    await panelCard(page,copyId).locator('[data-edit-panel]').click();
    let form = panelForm(page);
    assert.equal(await form.getByRole('tab',{name:'Data',exact:true}).getAttribute('aria-selected'),'true');
    await form.locator('[name="xAxis"]').selectOption('_runtime');
    await form.locator('[name="title"]').fill('Cancelled duplicate title');
    await form.getByRole('tab',{name:'Display',exact:true}).click();
    assert.equal(await form.getByRole('tab',{name:'Display',exact:true}).getAttribute('aria-selected'),'true');
    await form.locator('[name="maxRuns"]').fill('2');
    await form.locator('[name="smoothing"]').fill('0.6');
    await form.getByRole('button',{name:'Cancel',exact:true}).click();
    assert.equal(await panelCard(page,copyId).locator('.chart-title').textContent(),copyTitle);
    assert.equal((await panelSeries(page,copyId)).length,6);
    assert.deepEqual(await panelSeries(page,originalId),beforeDuplicate);
    await panelCard(page,copyId).locator('[data-edit-panel]').click();form=panelForm(page);
    await form.locator('[name="title"]').fill('Independent accuracy panel');
    await form.getByRole('tab',{name:'Display',exact:true}).click();
    await form.locator('[name="maxRuns"]').fill('2');await form.locator('[name="smoothing"]').fill('0.6');
    await form.getByRole('button',{name:'Apply settings',exact:true}).click();
    assert.equal(await panelCard(page,copyId).locator('.chart-title').textContent(),'Independent accuracy panel');
    assert.equal((await panelSeries(page,copyId)).length,2);assert.deepEqual(await panelSeries(page,originalId),beforeDuplicate);
    await checkpoint(page,'workflow-independent-scalar-duplicate','Scalar duplicate retains a distinct identity; Data/Display draft Cancel preserves both cards, and Apply changes only the copy');

    currentStage = 'workspace-section-panel-precedence';
    await page.locator('[data-action="workspace-settings"]').click();form=scopeForm(page);
    await form.locator('[name="maxRuns"]').fill('6');await form.locator('[name="smoothing"]').fill('0.4');
    await form.getByRole('button',{name:'Apply workspace settings',exact:true}).click();
    await assertPanelDefaults(page,originalId,6,.4);await assertPanelDefaults(page,copyId,2,.6);
    assert.equal((await panelSeries(page,originalId)).length,6);
    assert.notDeepEqual(await panelSeries(page,originalId),beforeDuplicate,'Workspace smoothing changes the inherited curve');
    await page.locator('[data-section-settings="train"]').click();form=scopeForm(page);
    await form.locator('[name="maxRuns"]').fill('4');await form.locator('[name="smoothing"]').fill('0.2');
    await form.getByRole('button',{name:'Apply section',exact:true}).click();
    await assertPanelDefaults(page,originalId,4,.2);await assertPanelDefaults(page,copyId,2,.6);
    assert.equal((await panelSeries(page,originalId)).length,4);assert.equal((await panelSeries(page,copyId)).length,2);
    await panelCard(page,originalId).locator('[data-edit-panel]').click();form=panelForm(page);
    await form.getByRole('tab',{name:'Display',exact:true}).click();
    await form.locator('[name="maxRuns"]').fill('3');await form.locator('[name="smoothing"]').fill('0.8');
    await form.getByRole('button',{name:'Apply settings',exact:true}).click();
    assert.equal((await panelSeries(page,originalId)).length,3);
    await page.locator('[data-action="workspace-settings"]').click();form=scopeForm(page);
    await form.locator('[name="maxRuns"]').fill('5');await form.locator('[name="smoothing"]').fill('0.7');
    await form.getByRole('button',{name:'Apply workspace settings',exact:true}).click();
    await assertPanelDefaults(page,originalId,3,.8);await assertPanelDefaults(page,copyId,2,.6);
    const overridden = await panelSeries(page,originalId);
    await panelCard(page,originalId).locator('[data-edit-panel]').click();form=panelForm(page);
    await form.getByRole('tab',{name:'Display',exact:true}).click();
    await form.getByRole('button',{name:'Use inherited defaults',exact:true}).click();
    assert.equal(Number(await form.locator('[name="maxRuns"]').inputValue()),4);
    assert.equal(Number(await form.locator('[name="smoothing"]').inputValue()),.2);
    await form.getByRole('button',{name:'Cancel',exact:true}).click();
    assert.deepEqual(await panelSeries(page,originalId),overridden,'Reset followed by Cancel retains saved panel overrides');
    await panelCard(page,originalId).locator('[data-edit-panel]').click();form=panelForm(page);
    await form.getByRole('button',{name:'Use inherited defaults',exact:true}).click();
    await form.getByRole('button',{name:'Apply settings',exact:true}).click();
    await assertPanelDefaults(page,originalId,4,.2);assert.equal((await panelSeries(page,originalId)).length,4);
    await page.locator('[data-section-settings="train"]').click();form=scopeForm(page);
    await form.locator('[name="maxRuns"]').fill('');await form.locator('[name="smoothing"]').fill('');
    await form.getByRole('button',{name:'Apply section',exact:true}).click();
    await assertPanelDefaults(page,originalId,5,.7);assert.equal((await panelSeries(page,originalId)).length,5);
    const inherited = await panelSeries(page,originalId);
    for(const action of ['workspace','section']){
      await page.locator(action==='workspace'?'[data-action="workspace-settings"]':'[data-section-settings="train"]').click();form=scopeForm(page);
      await form.locator('[name="maxRuns"]').fill('1');await form.locator('[name="smoothing"]').fill('0.12');
      await form.getByRole('button',{name:'Cancel',exact:true}).click();
      assert.deepEqual(await panelSeries(page,originalId),inherited,`${action} Cancel does not alter live charts`);
    }
    await assertPanelDefaults(page,copyId,2,.6);
    await checkpoint(page,'workflow-settings-precedence','Workspace defaults flow to sections and panels; explicit overrides win, reset needs Apply, and every scope Cancel preserves rendered charts');

    currentStage = 'run-comparer-visible-columns';
    await page.locator('[data-state="search"]').fill('');
    await page.locator('[data-action="add-panels"]').click();
    await page.locator('#inspect-dialog[open]').getByRole('button',{name:'Run comparer',exact:true}).click();
    await page.locator('#inspect-dialog[open] [data-library-close]').click();
    await page.locator('[data-state="metricSearch"]').fill('Run comparer');
    const comparer = page.locator('#app article.run-comparer');
    assert.equal(await comparer.count(),1);assert((await comparer.innerText()).includes('First 10 of 256 visible runs'));
    const comparerId = await comparer.getAttribute('data-metric');
    const firstTen = Array.from({length:10},(_,index)=>`${fixture.manifest.projectId}/workflow-${String(255-index).padStart(4,'0')}`);
    const comparerKeys=()=>panelCard(page,comparerId).locator('thead [data-run]').evaluateAll(nodes=>nodes.map(node=>node.dataset.run));
    assert.deepEqual(await comparerKeys(),firstTen);
    await page.locator('[data-action="expand-run-table"]').click();
    await tableSelection(page,firstTen[0]).check();
    await page.locator('[data-action="collapse-run-table"]').click();
    assert.deepEqual(await comparerKeys(),firstTen,'Checkbox selection cannot select comparer columns');
    await sidebarEye(page,firstTen[0]).click();
    assert.deepEqual(await comparerKeys(),[...firstTen.slice(1),`${fixture.manifest.projectId}/workflow-0245`]);
    await sidebarEye(page,firstTen[0]).click();assert.deepEqual(await comparerKeys(),firstTen);
    await comparer.locator('[data-comparer-option="search"]').fill('accelerator_count');
    assert((await comparer.innerText()).includes('Not logged'),'Missing configuration remains distinct from a numeric value');
    await comparer.locator('[data-comparer-option="search"]').fill('optional_evaluation');
    assert((await comparer.innerText()).includes('null'));assert((await comparer.innerText()).includes('Not logged'));
    await comparer.locator('[data-comparer-option="search"]').fill('seed');
    await comparer.locator('[data-comparer-option="differences"]').check();
    assert(await comparer.locator('.diff-cell').count()>0);
    await checkpoint(page,'workflow-run-comparer','Run comparer uses the first ten visible runs independently of row selection and preserves missing versus null recorded values');

    currentStage = 'visible-versus-pinned-run-columns';
    await page.locator('[data-action="run-columns"]').click();
    const columnsDialog=page.locator('#inspect-dialog[open]');
    assert.equal(await columnsDialog.locator('[data-run-column="run:name"]').count(),0,'Name always remains visible');
    for(const field of ['id','state','group','tags','created_at','updated_at','finished_at','job_type','notes','description','runtime'])assert.equal(await columnsDialog.locator(`[data-run-column="run:${field}"]`).count(),1);
    for(const field of ['config:seed','config:learning_rate','config:accelerator_count'])assert.equal(await columnsDialog.locator(`[data-run-column=${selectorKey(field)}]`).count(),1);
    const visible=columnsDialog.locator('[data-run-column="config:seed"]'), pinned=columnsDialog.locator('[data-pin-column="config:seed"]');
    await visible.uncheck();assert(await pinned.isDisabled());assert(!(await pinned.isChecked()));
    await visible.check();assert(await pinned.isEnabled());await pinned.check();
    await columnsDialog.getByRole('button',{name:'Done',exact:true}).click();
    assert.equal(await page.locator('.run-item [data-pinned-field="config:seed"]').count(),20);
    assert.equal(await page.locator('.run-list-columns [data-sort="config:seed"]').count(),1);
    await page.locator('[data-action="expand-run-table"]').click();
    assert.equal(await page.locator('.runs-table thead [data-sort="config:seed"]').count(),1);
    await page.locator('[data-action="collapse-run-table"]').click();
    await page.locator('[data-action="run-columns"]').click();
    await page.locator('#inspect-dialog[open] [data-pin-column="config:seed"]').uncheck();
    await page.locator('#inspect-dialog[open]').getByRole('button',{name:'Done',exact:true}).click();
    assert.equal(await page.locator('.run-item [data-pinned-field="config:seed"]').count(),0);
    await page.locator('[data-action="expand-run-table"]').click();
    assert.equal(await page.locator('.runs-table thead [data-sort="config:seed"]').count(),1,'Unpinning leaves full-table visibility intact');
    await page.locator('[data-action="collapse-run-table"]').click();
    await checkpoint(page,'workflow-run-column-scopes','All recorded metadata is discoverable; full-table visibility enables pinning, and unpinning removes only sidebar values');

    currentStage = 'panel-bank-save-reload';
    await durable.assertNoWrites({after:beforeDraft,scope});
    const beforeSave=durable.writes().length;
    await saveViewButton(page).click();
    const saved=await durable.waitForWrite({after:beforeSave,scope,predicate:event=>event.payload.snapshot.layout.panels.some(panel=>panel.id===copyId)});
    await waitForWorkspaceStatus(page,'Saved');
    const reader=await openWorkspaceFixture({viewId:scope.viewId,writeGrant:false});
    try{
      assert.equal(await reader.page.locator('#app article.run-comparer').count(),1);
      await reader.page.locator('[data-state="metricSearch"]').fill(visibilityMetric);
      assert.equal((await panelSeries(reader.page,originalId)).length,5);assert.equal((await panelSeries(reader.page,copyId)).length,2);
      await assertPanelDefaults(reader.page,originalId,5,.7);await assertPanelDefaults(reader.page,copyId,2,.6);
      await reader.page.locator('[data-state="metricSearch"]').fill('samples/');
      assert.equal(await panelCard(reader.page,addedImageId).count(),1);assert.equal(await panelCard(reader.page,addedTableId).count(),1);
      assert.equal(durable.latest(scope).revisionId,saved.event.event_id);
      await checkpoint(reader.page,'workflow-panel-bank-save-reload','Named Save restores independent rich/scalar/comparer identities and chart inheritance in a fresh read-only browser');
    }catch(error){await recordFailure(error);throw error;}
    finally{await reader.context.close();activePage=page;}
  }catch(error){await recordFailure(error);throw error;}
  finally{await writer.context.close();}
}

/** Validate the first-release analysis path without custom workspace controls.
 * Main callers: default runner after the independent selection/visibility path.
 * Why this exists: automatically discovered panels and ordinary run exploration
 * are the release scope. Mutation tests above stay advanced and opt-in. Hover
 * values are checked against fixture events, never reconstructed UI state.
 */
async function verifyDefaultAnalysisJourney(page) {
  currentStage='default-release-scope';
  await page.locator('[data-state="search"]').fill('');
  await page.locator('[data-state="metricSearch"]').fill('');
  await workspaceIdentity(page);
  for(const selector of ['[data-view-choice]','[data-workspace-status]','[data-action="save-view"]','[data-action="save-view-as"]','[data-action="add-panels"]','[data-action="workspace-settings"]','[data-edit-panel]','[data-panel-drag]','[data-panel-resize]','[data-section-settings]','[data-section-menu]','[data-section-drag]','[data-section-pin]'])assert.equal(await page.locator(`#app ${selector}`).count(),0,`${selector} is outside the default release scope`);
  assert(!requests.some(request=>request.pathname==='/multipass/api/me'),'Default browsing must not read workspace identity');
  assert.equal(durable.writes().length,0,'Default browsing cannot publish workspace snapshots');
  await assertCompactRunRows(page);
  currentStage='automatic-scalar-discovery';
  const discovered=[];
  for(const sectionName of WORKFLOW_SECTIONS){
    const section=page.locator(`[data-section-id=${selectorKey(sectionName)}]`);
    const expected=WORKFLOW_METRICS.filter(key=>key.startsWith(sectionName+'/'));
    assert((await section.locator('[data-section]').innerText()).includes(`${expected.length+(sectionName==='evaluation'?2:0)} panels`));
    const first=await section.locator('article[data-metric]').evaluateAll(nodes=>nodes.map(node=>node.dataset.metric));
    discovered.push(...first.filter(key=>expected.includes(key)));
    await section.locator('[data-section-page][data-page-delta="1"]').click();
    const second=await section.locator('article[data-metric]').evaluateAll(nodes=>nodes.map(node=>node.dataset.metric));
    discovered.push(...second.filter(key=>expected.includes(key)));
    if(sectionName==='evaluation'){
      await section.locator('[data-section-page][data-page-delta="1"]').click();
      discovered.push(...await section.locator('article[data-metric]').evaluateAll(nodes=>nodes.map(node=>node.dataset.metric)).then(keys=>keys.filter(key=>expected.includes(key))));
      await section.locator('[data-section-page][data-page-delta="-1"]').click();
    }
    await section.locator('[data-section-page][data-page-delta="-1"]').click();
  }
  assert.deepEqual([...new Set(discovered)].sort(),[...WORKFLOW_METRICS].sort(),'All 96 recorded scalar keys are automatically reachable through section pages');
  await page.locator('[data-section="train"]').click();
  assert.equal(await page.locator('[data-section="train"]').getAttribute('aria-expanded'),'false');
  assert.equal(await page.locator('[data-section-id="train"] article').count(),0);
  await page.locator('[data-section="train"]').click();
  assert.equal(await page.locator('[data-section="train"]').getAttribute('aria-expanded'),'true');
  await checkpoint(page,'workflow-default-scalar-discovery','All 96 scalar keys are automatically reachable; section paging and collapse preserve their recorded identities');

  currentStage='default-rich-output-inspection';
  await page.locator('[data-state="search"]').fill('experiment-000');
  await page.locator('[data-state="metricSearch"]').fill('samples/');
  assert.equal(await page.locator('#app article.logged-panel').count(),3);
  const image=page.locator('#app article[data-panel-type="image"][data-source="samples/image"]');
  const table=page.locator('#app article[data-panel-type="table"][data-source="samples/predictions"]');
  const gallery=page.locator('#app article[data-panel-type="media"][data-source="samples/gallery"]');
  for(const panel of [image,table,gallery])assert.equal(await panel.locator('.logged-panel-entry').count(),8);
  await image.locator('.image-viewer').first().waitFor();
  assert.equal(await table.locator('.logged-table').count(),8);
  await table.locator('[data-table-option][data-option="limit"]').first().selectOption('25');
  assert((await table.innerText()).includes('1-25 of 96'));
  await table.locator('[data-inspect-table][data-row="0"]').first().click();
  const rowDialog=page.locator('#inspect-dialog[open]');
  assert((await rowDialog.innerText()).includes('Repeated context makes full row inspection necessary.'));
  assert((await rowDialog.locator('.row-fields').innerText()).length>1200,'Row inspection exposes the full long prompt');
  await rowDialog.getByRole('button',{name:'Close row inspector',exact:true}).click();
  for(const [key,type] of [['evaluation/confidence_distribution','histogram'],['evaluation/class_accuracy','plot']]){
    await page.locator('[data-state="metricSearch"]').fill(key);
    assert.equal(await page.locator(`#app article[data-panel-type=${selectorKey(type)}] .logged-panel-entry`).count(),8);
  }
  await checkpoint(page,'workflow-default-rich-discovery','Automatic image/table/gallery/histogram/plot panels show recorded data, including full long table rows');

  currentStage='default-chart-point-inspection';
  await page.locator('[data-state="search"]').fill('experiment-025');
  await page.locator('[data-state="metricSearch"]').fill(visibilityMetric);
  const before=await panelSeries(page,visibilityMetric);
  assert.equal(before.length,6);
  const svg=panelCard(page,visibilityMetric).locator('.chart-svg');
  const box=await svg.boundingBox();assert(box);
  await page.mouse.move(box.x+box.width*.55,box.y+box.height*.5);
  const tooltip=panelCard(page,visibilityMetric).locator('.chart-tooltip');
  await tooltip.waitFor({state:'visible'});
  const samples=await tooltip.locator('.tooltip-series').evaluateAll(nodes=>nodes.map(node=>({name:node.querySelector('span[title]').textContent,value:Number(node.querySelector('b').textContent),x:Number(node.nextElementSibling.textContent.match(/^X\s+([\d.]+)/)[1])})));
  assert.equal(samples.length,6);
  for(const sample of samples){
    const runId=Object.entries(fixture.manifest.runNames).find(([,name])=>name===sample.name)?.[0];assert(runId);
    const row=fixture.events.find(event=>event.run_id===runId&&event.type==='history'&&event.payload.step===sample.x);
    assert(row&&Object.hasOwn(row.payload.metrics,visibilityMetric),'Every hovered point corresponds to a recorded fixture sample');
    assert(Math.abs(sample.value-row.payload.metrics[visibilityMetric])<.001,'Hover reports the recorded metric value within display rounding');
  }
  assert.deepEqual(await panelSeries(page,visibilityMetric),before,'Hover does not change chart series');
  await checkpoint(page,'workflow-default-chart-hover','Pointer hover exposes six recorded run values and sample coordinates without changing plotted series');
  await page.mouse.move(2,2);await tooltip.waitFor({state:'hidden'});
  await svg.focus();await svg.press('Home');await tooltip.waitFor({state:'visible'});
  assert.equal(await tooltip.locator('.tooltip-series').count(),6);await svg.press('Escape');await tooltip.waitFor({state:'hidden'});
  await panelCard(page,visibilityMetric).locator('[data-expand]').click();
  const expanded=page.locator('#chart-dialog[open]');
  assert.equal(await expanded.locator('.chart-svg path[stroke-width="1.8"]').count(),6);
  assert.equal((await expanded.locator('h2').textContent()).trim(),visibilityMetric);
  await expanded.getByRole('button',{name:'Close expanded chart',exact:true}).click();
  assert.deepEqual(await panelSeries(page,visibilityMetric),before,'Closing full screen restores the same workspace chart');
  const key=`${fixture.manifest.projectId}/workflow-0255`;
  const legend=panelCard(page,visibilityMetric).getByTitle(`Toggle ${fixture.manifest.runNames['workflow-0255']}`,{exact:true});
  await legend.click();assert.equal((await panelSeries(page,visibilityMetric)).length,5);
  assert.equal(await sidebarEye(page,key).getAttribute('aria-pressed'),'true','A panel legend filter does not change the run eye');
  await legend.click();assert.deepEqual(await panelSeries(page,visibilityMetric),before);
  const zoomBox=await svg.boundingBox();
  await page.mouse.move(zoomBox.x+zoomBox.width*.25,zoomBox.y+zoomBox.height*.5);await page.mouse.down();
  await page.mouse.move(zoomBox.x+zoomBox.width*.65,zoomBox.y+zoomBox.height*.5);await page.mouse.up();
  assert(await panelCard(page,visibilityMetric).locator('.zoom-note').isVisible());
  await panelCard(page,visibilityMetric).locator('[data-reset-chart]').click();
  assert.deepEqual(await panelSeries(page,visibilityMetric),before,'Reset zoom restores the chart without changing logged values');
  await checkpoint(page,'workflow-default-chart-inspection','Keyboard inspection, full screen, legend toggling and zoom reset preserve independent run visibility');

  currentStage='default-group-sort-exploration';
  await page.locator('[data-action="run-sort"]').click();
  let dialog=page.locator('#inspect-dialog[open]');
  await dialog.locator('[data-analysis="sort"]').selectOption('run:id');await dialog.locator('[data-analysis="direction"]').selectOption('1');
  await dialog.getByRole('button',{name:'Done',exact:true}).click();
  const cohort=Array.from({length:6},(_,index)=>`${fixture.manifest.projectId}/workflow-${String(250+index).padStart(4,'0')}`);
  assert.deepEqual(await page.locator('.run-item [data-run]').evaluateAll(nodes=>nodes.map(node=>node.dataset.run)),cohort);
  await page.locator('[data-action="run-grouping"]').click();dialog=page.locator('#inspect-dialog[open]');
  await dialog.locator('[data-analysis="groupBy"]').selectOption('run:group');await dialog.getByRole('button',{name:'Done',exact:true}).click();
  assert.equal(await page.locator('.sidebar-group').count(),6);assert.equal((await panelSeries(page,visibilityMetric)).length,6);
  await page.locator('[data-action="expand-run-table"]').click();
  assert.equal(await page.locator('.runs-table .run-group-row').count(),6);
  const visibleKeys=await page.locator('.runs-table tbody [data-run-select]').evaluateAll(nodes=>nodes.map(node=>node.dataset.runSelect));
  assert.deepEqual([...visibleKeys].sort(),[...cohort].sort());
  const selected=visibleKeys[0];await tableSelection(page,selected).check();
  await page.getByRole('group',{name:'Selected run actions',exact:true}).getByRole('button',{name:'Hide from charts',exact:true}).click();
  assert.equal(await tableEye(page,selected).getAttribute('aria-pressed'),'false');assert(await tableSelection(page,selected).isChecked());
  await page.getByRole('group',{name:'Selected run actions',exact:true}).getByRole('button',{name:'Show in charts',exact:true}).click();
  assert.equal(await tableEye(page,selected).getAttribute('aria-pressed'),'true');assert(await tableSelection(page,selected).isChecked());
  await tableSelection(page,selected).uncheck();
  await page.locator('[data-action="collapse-run-table"]').click();
  await page.locator('[data-action="run-grouping"]').click();dialog=page.locator('#inspect-dialog[open]');
  await dialog.locator('[data-analysis="groupBy"]').selectOption('');await dialog.getByRole('button',{name:'Done',exact:true}).click();
  await checkpoint(page,'workflow-default-group-sort','Recorded run ID sorting and group headings agree across sidebar/table; bulk Show/Hide retain independent row selection');

  currentStage='default-visible-pinned-columns';
  await page.locator('[data-action="run-columns"]').click();dialog=page.locator('#inspect-dialog[open]');
  assert.equal(await dialog.locator('[data-run-column="run:name"]').count(),0);
  for(const field of ['id','state','group','tags','created_at','updated_at','finished_at','job_type','notes','description','runtime'])assert.equal(await dialog.locator(`[data-run-column="run:${field}"]`).count(),1);
  const visible=dialog.locator('[data-run-column="config:seed"]'),pinned=dialog.locator('[data-pin-column="config:seed"]');
  await visible.uncheck();assert(await pinned.isDisabled());await visible.check();assert(await pinned.isEnabled());await pinned.check();
  await dialog.getByRole('button',{name:'Done',exact:true}).click();
  assert.equal(await page.locator('.run-item [data-pinned-field="config:seed"]').count(),6);
  await assertCompactRunRows(page);
  await page.locator('[data-action="expand-run-table"]').click();
  assert.equal(await page.locator('.runs-table thead [data-sort="config:seed"]').count(),1);
  await page.locator('[data-action="collapse-run-table"]').click();await page.locator('[data-action="run-columns"]').click();
  dialog=page.locator('#inspect-dialog[open]');await dialog.locator('[data-pin-column="config:seed"]').uncheck();await dialog.getByRole('button',{name:'Done',exact:true}).click();
  assert.equal(await page.locator('.run-item [data-pinned-field="config:seed"]').count(),0);
  await page.locator('[data-action="expand-run-table"]').click();assert.equal(await page.locator('.runs-table thead [data-sort="config:seed"]').count(),1);
  await page.locator('[data-action="collapse-run-table"]').click();
  await checkpoint(page,'workflow-default-run-columns','All metadata is discoverable; full-table visibility and sidebar pinning have separate, observable effects');
  currentStage='local-exploration-reload';
  const hiddenKey=cohort[0];await sidebarEye(page,hiddenKey).click();
  const beforeReload=await panelSeries(page,visibilityMetric);assert.equal(beforeReload.length,5);
  await page.reload();await page.locator('.run-item').first().waitFor({timeout:30000});
  await workspaceIdentity(page);
  assert.equal(await page.locator('[data-state="search"]').inputValue(),'experiment-025');
  assert.equal(await page.locator('[data-state="metricSearch"]').inputValue(),visibilityMetric);
  assert.equal(await sidebarEye(page,hiddenKey).getAttribute('aria-pressed'),'false');
  assert.deepEqual(await panelSeries(page,visibilityMetric),beforeReload,'Reload recovers browser exploration choices without inventing a shared save');
  assert.deepEqual(await page.locator('.run-item [data-run]').evaluateAll(nodes=>nodes.map(node=>node.dataset.run)),cohort,'Run sort recovers with local exploration');
  assert(await page.evaluate(()=>localStorage.getItem('backprop.exploration.v1')!==null&&localStorage.getItem('backprop.workspace.v1')===null),'Default exploration uses its separate, explicit browser key');
  await sidebarEye(page,hiddenKey).click();
  await checkpoint(page,'workflow-local-exploration-reload','Search, panel search, sorting and a hidden eye recover on reload using browser exploration storage, with no shared save claim');
  assert.equal(durable.writes().length,0,'Default inspection stays outside the advanced save transport');
}

/** Browse immutable project artifacts and return from their recorded run links.
 * Main callers: default analysis runner. Version identity, metadata and edges
 * derive from the fixture manifest. Table content uses real authenticated file
 * reads; a direct artifact URL must recover without previous selection state.
 */
async function verifyProjectArtifactJourney(page) {
  currentStage='project-artifact-versions';
  await page.getByRole('navigation',{name:'Project views',exact:true}).getByRole('button',{name:'Artifacts',exact:true}).click();
  assert.equal(new URL(page.url()).searchParams.get('view'),'artifacts');
  assert.equal(await page.locator('[data-artifact-collection]').count(),1);
  const collection=page.locator('[data-artifact-collection]');assert((await collection.innerText()).includes('8 versions'));
  await collection.click();
  const version=page.locator('[data-artifact-version]');assert.equal(await version.locator('option').count(),8);
  const firstVersion=await version.locator('option').evaluateAll(nodes=>nodes.find(node=>/^v0(?:\s|$)/.test(node.textContent))?.value);assert(firstVersion);
  await version.selectOption(firstVersion);
  const content=page.locator('#artifact-tab-content');
  assert((await content.innerText()).includes('workflow-evaluation-0000'));assert((await content.innerText()).includes('cohort-0'));
  assert((await content.innerText()).includes('demo-user/workflow-reference-fixture/workflow-evaluation:v0'));
  await checkpoint(page,'workflow-project-artifact-versions','One artifact collection exposes eight immutable versions with their recorded IDs, aliases and qualified names');
  currentStage='project-artifact-tab-inspection';
  const producer=`${fixture.manifest.projectId}/workflow-0000`;
  for(const tab of ['metadata','usage','files','lineage']){
    await page.locator(`[data-artifact-tab=${selectorKey(tab)}]`).click();
    assert.equal(await page.locator(`[data-artifact-tab=${selectorKey(tab)}]`).getAttribute('aria-selected'),'true');
    assert.equal(new URL(page.url()).searchParams.get('artifactTab'),tab);
    if(tab==='metadata')assert.deepEqual(JSON.parse(await content.locator('pre').textContent()),{rows:96,synthetic:true});
    if(tab==='usage'||tab==='lineage')assert.equal(await content.locator(`[data-run=${selectorKey(producer)}]`).count(),2,'The selected version retains its one producer and recorded consumer');
    if(tab==='files'){
      assert((await content.innerText()).includes('evaluation.table.json'));
      await content.getByRole('button',{name:'Open table',exact:true}).click();
      const explorer=page.locator('#table-dialog[open]');
      await explorer.locator('.logged-table tbody tr').first().waitFor();
      await explorer.locator('[data-table-option][data-option="limit"]').selectOption('25');
      assert.equal(await explorer.locator('.logged-table tbody tr').count(),25);
      assert((await explorer.innerText()).includes('1-25 of 96'));
      await explorer.locator('[data-inspect-table][data-row="0"]').click();
      assert((await explorer.innerText()).includes('Repeated context makes full row inspection necessary.'));
      await explorer.getByRole('button',{name:'Close table explorer',exact:true}).click();
      assert.equal(new URL(page.url()).searchParams.get('artifactTab'),'files');
    }
  }
  const artifactTabs=page.getByRole('tablist',{name:'Artifact details',exact:true});
  assert.equal(await artifactTabs.locator('[tabindex="0"]').count(),1);
  await artifactTabs.getByRole('tab',{name:'Lineage',exact:true}).focus();
  await page.keyboard.press('ArrowLeft');assert.equal(await page.locator('[data-artifact-tab="files"]').getAttribute('aria-selected'),'true');
  await page.keyboard.press('End');assert.equal(await page.locator('[data-artifact-tab="lineage"]').getAttribute('aria-selected'),'true');
  assert(requests.some(request=>request.pathname.includes('/files/')&&request.pathname.includes('workflow-0000')),'Artifact table inspection must read its immutable file bytes');
  await checkpoint(page,'workflow-project-artifact-tabs','Metadata, usage, files and lineage inspect recorded version data; artifact table drilldown reads verified immutable bytes');
  currentStage='artifact-run-inspection-return';
  const artifactUrl=page.url();
  await content.locator(`[data-run=${selectorKey(producer)}]`).last().click();
  assert.equal(new URL(page.url()).searchParams.get('view'),'run');assert.equal(new URL(page.url()).searchParams.get('run'),'workflow-0000');
  await workspaceIdentity(page);
  const runNav=page.getByRole('navigation',{name:'Run details',exact:true});
  await runNav.getByRole('button',{name:'Overview',exact:true}).click();
  assert((await page.locator('.run-page-content').innerText()).includes('synthetic-classification'));
  await runNav.getByRole('button',{name:'Logs',exact:true}).click();
  assert((await page.locator('.run-page-content').innerText()).includes('Synthetic training session 0000'));
  await runNav.getByRole('button',{name:'Files',exact:true}).click();assert((await page.locator('.run-page-content').innerText()).includes('training.txt'));
  await runNav.getByRole('button',{name:'Artifacts',exact:true}).click();assert((await page.locator('.run-page-content').innerText()).includes('workflow-evaluation'));
  await page.getByRole('button',{name:'Exit single-run view',exact:true}).click();
  assert.equal(page.url(),artifactUrl,'Run exit retains artifact collection, immutable version and active tab');
  assert.equal(await page.locator('[data-artifact-version]').inputValue(),firstVersion);
  assert.equal(await page.locator('[data-artifact-tab="lineage"]').getAttribute('aria-selected'),'true');
  const reader=await createFixturePage({writeGrant:false});
  try{
    await reader.page.goto(artifactUrl);await reader.page.locator('#artifact-tab-content').waitFor({timeout:30000});
    assert.equal(await reader.page.locator('[data-artifact-version]').inputValue(),firstVersion);
    assert.equal(await reader.page.locator('[data-artifact-tab="lineage"]').getAttribute('aria-selected'),'true');
    assert.equal(await reader.page.locator(`#artifact-tab-content [data-run=${selectorKey(producer)}]`).count(),2);
    await checkpoint(reader.page,'workflow-artifact-run-return','Run Overview/Logs/Files/Artifacts inspect fixture data; exit and a fresh artifact URL recover the same immutable version and lineage');
  }catch(error){await recordFailure(error);throw error;}
  finally{await reader.context.close();activePage=page;}
  assert.equal(durable.writes().length,0,'Default artifact browsing cannot publish display snapshots');
}

/** Boot a real release document over an empty archive/live stream.
 * Main callers: the default runner. This catches eager workspace-session calls
 * when there is no project, while retaining ordinary onboarding and API reads.
 */
async function verifyEmptyDefaultWorkspace() {
  currentStage='empty-default-workspace';
  const previousPage=activePage;
  const empty=await createFixturePage({empty:true});
  try{
    await empty.page.goto(origin+'/?view=workspace');
    await empty.page.getByRole('heading',{name:'Your next experiment starts here.',exact:true}).waitFor({timeout:30000});
    assert.equal(await empty.page.locator('.run-item').count(),0);assert.equal(await empty.page.locator('.chart-svg').count(),0);
    assert(await empty.page.getByRole('button',{name:'Connect your code',exact:true}).last().isVisible());
    assert(!(await empty.page.locator('#app').innerText()).includes('Could not load'));
    assert.equal(await empty.page.locator('[data-workspace-status]').count(),0);assert.equal(await empty.page.locator('[data-view-choice]').count(),0);
    assert(!requests.some(request=>request.pathname==='/multipass/api/me'));assert.equal(durable.writes().length,0);
    await checkpoint(empty.page,'workflow-empty-default-workspace','An empty archive/live stream boots onboarding without project, workspace-session or save errors');
  }catch(error){await recordFailure(error);throw error;}
  finally{await empty.context.close();activePage=previousPage;}
}

try {
  await mkdir(output, {recursive: true});
  await rm(join(output, 'workflow-failure.png'), {force: true});
  browser = await playwright.chromium.launch({headless: true, executablePath: process.env.CHROMIUM_EXECUTABLE});
  try {
    if (saveTransportOnly) await verifyDurableWorkspaceTransport();
    else if(sharedViews||panels){
      assert(CUSTOM_WORKSPACES,'Advanced UI checks require an explicitly enabled custom workspace build; this default release excludes that layer.');
      if(sharedViews)await verifySharedWorkspaceJourney();
      if(panels)await verifyPanelJourney();
    }
    else {
      const {context, page} = await createFixturePage();
      await verifyTransport(page);
      if (!smokeOnly) {
        await verifyReferenceJourney(page);
        await verifyDefaultAnalysisJourney(page);
        await verifyProjectArtifactJourney(page);
        await verifyEmptyDefaultWorkspace();
      }
      await context.close();
    }
    assert.deepEqual(pageErrors, [], 'Browser errors cannot be hidden by a passing UI assertion');
    assert.deepEqual(transportErrors, [], 'All fixture transport requests must be understood');
    evidence.complete = true;
    // Complete here means this runner's chosen mode, not the broader parity goal.
    evidence.completionScope = saveTransportOnly ? 'workspace transport only; no save UI parity claim'
      : smokeOnly ? 'fixture transport only'
        : extraJourneys.length ? `only gated ${extraJourneys.join(', ')} checks; this is outside the default release checkpoint`
          : 'default automatic analysis, independent run exploration, chart inspection and project artifacts; custom workspace layers remain excluded';
  } catch (error) {
    await recordFailure(error);
    throw error;
  }
} finally {
  evidence.requests = requests;
  evidence.pageErrors = pageErrors;
  evidence.transportErrors = transportErrors;
  evidence.workspaceWrites = durable.writes();
  evidence.savedWorkspaceRevisions = durable.workspaceEvents().map(event => ({eventId: event.event_id, scope: event.payload.scope, parents: event.payload.parents, name: event.payload.name}));
  await mkdir(output, {recursive: true});
  await writeFile(join(output, 'evidence.json'), JSON.stringify(evidence, null, 2) + '\n');
  if (browser) await browser.close();
  await new Promise(resolveServer => server.close(resolveServer));
}
