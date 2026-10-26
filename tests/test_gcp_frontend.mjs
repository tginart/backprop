/**
 * Purpose: Verify Google browser authentication through the shared adapter interface.
 * Input: Google popup, storage, time, and verified-identity fixtures.
 * Output: Scoped sessions, renewal, sign-out, and strict connection validation.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import {createAdapter} from '../web/backends/gcp.js';
import {exportConfiguration} from '../web/backends/connection.js';

const CONFIG = {backend: 'gcp', host: 'https://gcp.example', gcp_id: 'a'.repeat(32),
    client_id: 'public-google-client', stream_dataset_rid: 'ri.foundry.gcp.dataset.events',
    media_dataset_rid: 'ri.foundry.gcp.dataset.media', branch: 'master'};

function fixture() {
    const values = new Map();
    globalThis.location = {origin: CONFIG.host, href: CONFIG.host + '/?project=demo',
        assign() {}, reload() {}};
    globalThis.sessionStorage = {getItem: key => values.get(key) || null,
        setItem: (key, value) => values.set(key, value), removeItem: key => values.delete(key)};
    let client;
    let calls = 0;
    globalThis.google = {accounts: {oauth2: {initTokenClient(options) {
        client = {...options, requestAccessToken() {
            calls += 1;
            client.callback({access_token: 'google-fixture', expires_in: 3600});
        }};
        return client;
    }}}};
    globalThis.fetch = async (url, options) => {
        assert.equal(url, CONFIG.host + '/api/backprop/identity');
        assert.equal(options.headers.Authorization, 'Bearer google-fixture');
        assert.equal(options.redirect, 'error');
        return {ok: true, json: async () => ({userId: 'google-user', displayName: 'operator@example.com'})};
    };
    return {values, calls: () => calls, client: () => client};
}

test('Google session verifies identity, survives reload, renews, and signs out', async () => {
    const state = fixture();
    const adapter = createAdapter();
    adapter.validateConfig(CONFIG);
    assert.equal((await adapter.initAuth(CONFIG)).authenticated, false);
    assert.equal(await adapter.getAccessToken(), null);
    const [first, second] = await Promise.all([adapter.login(), adapter.login()]);
    assert.equal(first.user, 'operator@example.com');
    assert.deepEqual(first, second);
    assert.equal(state.calls(), 1);
    assert(first.canControlRuns && first.canSaveWorkspaces);
    const restored = createAdapter();
    assert.equal((await restored.initAuth(CONFIG)).authenticated, true);
    assert.equal(await restored.getAccessToken(), 'google-fixture');
    const now = Date.now;
    Date.now = () => now() + 3600000;
    try { assert.equal(await restored.getAccessToken(), 'google-fixture'); }
    finally { Date.now = now; }
    assert.equal(state.calls(), 2);
    restored.logout();
    assert.equal(restored.getAuthState().authenticated, false);
    assert.equal(state.values.size, 0);
});

test('Google denied identities, popup errors, and malformed sessions fail closed', async () => {
    const state = fixture();
    state.values.set(`backprop.google.session.${CONFIG.gcp_id}`, '{bad-json');
    const adapter = createAdapter();
    await adapter.initAuth(CONFIG);
    globalThis.fetch = async () => ({ok: false});
    await assert.rejects(adapter.login(), /does not have access/);
    assert.equal(adapter.getAuthState().canControlRuns, false);
    state.client().requestAccessToken = () => state.client().error_callback();
    await assert.rejects(adapter.login(), /cancelled or blocked/);
    assert.equal(await adapter.getAccessToken(), null);
});

test('Google connection export omits secrets and rejects unsafe origins and boundaries', () => {
    fixture();
    const adapter = createAdapter();
    assert.deepEqual(exportConfiguration({...CONFIG, access_token: 'secret', project: 'private'}), CONFIG);
    for (const change of [{host: 'http://gcp.example'}, {host: 'https://foreign.example'},
        {host: 'https://user@gcp.example'}, {gcp_id: ''}, {branch: 'other'},
        {stream_dataset_rid: 'ri.foundry.main.dataset.other'}]) {
        assert.throws(() => adapter.validateConfig({...CONFIG, ...change}), /invalid/);
    }
    assert.deepEqual(adapter.getArchiveOffsets({headers: new Headers({'X-Backprop-End-Offset': '42'})}), {'0': '42'});
    assert.throws(() => adapter.getArchiveOffsets({headers: new Headers()}), /boundary/);
});
