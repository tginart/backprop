/**
 * Purpose: Keep real Foundry credentials inside the test HTTP proxy.
 * Input: Browser context doubles and credential-bearing proxy requests.
 * Output: Exact-origin forwarding without browser credential disclosure.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import {configureFoundryBrowser} from './browser_foundry_proxy.mjs';

function contextFixture() {
    const routes = [], fetched = [], scripts = [];
    return {routes, fetched, scripts,
        addInitScript: async script => scripts.push(script),
        route: async (pattern, callback) => routes.push({pattern, callback}),
        request: {fetch: async (url, options) => {
            fetched.push({url, options});
            return {headers: () => ({'content-type': 'application/json'})};
        }},
    };
}

function requestFixture(url, method = 'GET') {
    const fulfilled = [];
    return {fulfilled,
        request: () => ({url: () => url, method: () => method,
            headers: () => ({authorization: 'Bearer browser-fixture-token'}), postDataBuffer: () => null}),
        fulfill: async result => fulfilled.push(result),
    };
}

test('real proxy credentials reach only the exact configured Foundry origin', async () => {
    const context = contextFixture();
    await configureFoundryBrowser(context, {origin: 'http://127.0.0.1:8765',
        config: {host: 'https://foundry.example'}, token: 'private-proxy-fixture'});
    const memory = new Map();
    globalThis.sessionStorage = {setItem: (key, value) => memory.set(key, value)};
    context.scripts[0]();
    assert(![...memory.values()].some(value => value.includes('private-proxy-fixture')));
    const handler = context.routes.at(-1).callback;
    await handler(requestFixture('https://foundry.example/api/v2/example'));
    assert.equal(context.fetched[0].url, 'https://foundry.example/api/v2/example');
    assert.equal(context.fetched[0].options.headers.Authorization, 'Bearer private-proxy-fixture');
    assert.equal(context.fetched[0].options.maxRedirects, 0);
    await assert.rejects(handler(requestFixture('https://foreign.example/api/v2/example')), /foreign credential destination/);
    assert.equal(context.fetched.length, 1);
});

test('fixture proxy forwards to loopback without a credential and handles preflight locally', async () => {
    const context = contextFixture();
    await configureFoundryBrowser(context, {origin: 'http://127.0.0.1:8765', config: {host: 'https://foundry.example'}});
    const handler = context.routes.at(-1).callback;
    await handler(requestFixture('https://foundry.example/api/v2/example?preview=true'));
    assert.equal(context.fetched[0].url, 'http://127.0.0.1:8765/api/v2/example?preview=true');
    assert.equal(context.fetched[0].options.headers.Authorization, undefined);
    const options = requestFixture('https://foundry.example/api/v2/example', 'OPTIONS');
    await handler(options);
    assert.equal(options.fulfilled[0].status, 204);
    assert.equal(context.fetched.length, 1);
});
