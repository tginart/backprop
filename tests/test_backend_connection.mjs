/**
 * Purpose: Use the same connection fixtures as the SDK for browser backend selection.
 * Input: Both public configurations and browser origin doubles.
 * Output: Matching selection, public config export, and strict connection policy.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {initAuth, validateConfiguration, getBackendPresentation,
    getAccessToken, exportConfiguration} from '../web/backends/connection.js';

const connections = JSON.parse(await readFile(new URL('./fixtures/backend_connections.json', import.meta.url)));

for (const [name, config] of Object.entries(connections)) {
    test(`one ${name} config selects the browser adapter and exports SDK settings`, async () => {
        globalThis.location = {href: config.app_url, origin: new URL(config.app_url).origin};
        globalThis.sessionStorage = {getItem: () => null};
        validateConfiguration(config);
        await initAuth(config);
        assert.equal(getBackendPresentation().label.toLowerCase(), name);
        assert.deepEqual(exportConfiguration({...config, token: 'must-not-export', client_secret: 'must-not-export'}), config);
        if (name === 'local') assert.equal(await getAccessToken(), null);
    });
}

test('unknown selectors and insecure Foundry or foreign local origins fail', async () => {
    for (const backend of ['other', null]) {
        assert.throws(() => validateConfiguration({...connections.foundry, backend}), /Unknown Backprop backend/);
    }
    assert.throws(() => validateConfiguration({...connections.foundry, host: 'http://foundry.example'}), /invalid/);
    globalThis.location = {origin: 'http://127.0.0.1:9999'};
    assert.throws(() => validateConfiguration(connections.local), /invalid/);
});
