/**
 * Architecture: browser storage/location doubles -> real OAuth module ->
 * asserted PKCE requests and callbacks. Only HTTP token exchange is mocked.
 * Main callers: node --test tests/test_auth.mjs. No secrets or network access.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import {webcrypto} from 'node:crypto';

const APP = 'https://backprop.example/';
const CONFIG = {host: 'https://foundry.example', client_id: 'test-client', app_url: APP};
let moduleSequence = 0;

async function browser(url = APP) {
  const memory = new Map(), exchanges = [], replaced = [];
  const location = {href: url, origin: new URL(url).origin, assign(value) { this.assigned = String(value); }};
  Object.defineProperty(globalThis, 'crypto', {value: webcrypto, configurable: true});
  globalThis.location = location;
  globalThis.history = {replaceState(_state, _title, target) {
    location.href = String(target); replaced.push(String(target));
  }};
  globalThis.sessionStorage = {
    getItem: key => memory.get(key) ?? null,
    setItem: (key, value) => memory.set(key, value),
    removeItem: key => memory.delete(key),
  };
  globalThis.fetch = async (url, options) => {
    exchanges.push({url, options});
    return new Response(JSON.stringify({access_token: 'test-token', refresh_token: 'test-refresh', expires_in: 3600}),
      {status: 200, headers: {'Content-Type': 'application/json'}});
  };
  const auth = await import(`../web/auth.js?test=${++moduleSequence}`);
  return {auth, memory, exchanges, replaced, location};
}

test('explicit local auth skips OAuth and preserves Foundry session storage', async () => {
  const env = await browser('http://127.0.0.1:8765/');
  env.memory.set('backprop.oauth.session', 'saved-foundry-session');
  await env.auth.initAuth({backend: 'local', host: 'http://127.0.0.1:8765'});
  assert.equal(env.auth.getAuthState().authenticated, true);
  assert.equal(env.auth.getAuthState().canControlRuns, true);
  assert.equal(await env.auth.getAccessToken(), null);
  await env.auth.login({allowRunControl: true});
  env.auth.logout();
  assert.equal(env.exchanges.length, 0);
  assert.equal(env.memory.get('backprop.oauth.session'), 'saved-foundry-session');
  assert.equal(env.location.assigned, undefined);
});

test('OAuth preserves the exact local run link after a validated PKCE callback', async () => {
  const original = `${APP}?project=demo-user%2Fsynthetic-project&run=run-123#metrics`;
  const env = await browser(original);
  await env.auth.initAuth(CONFIG);
  await env.auth.login();
  const pending = JSON.parse(env.memory.get('backprop.oauth.pending'));
  assert.equal(pending.returnUrl, original);
  const authorize = new URL(env.location.assigned);
  assert.equal(authorize.searchParams.get('redirect_uri'), APP);
  assert.equal(authorize.searchParams.get('state'), pending.state);
  assert.equal(authorize.searchParams.get('code_challenge_method'), 'S256');
  const digest = await webcrypto.subtle.digest('SHA-256', new TextEncoder().encode(pending.verifier));
  assert.equal(authorize.searchParams.get('code_challenge'), Buffer.from(digest).toString('base64url'));
  assert.deepEqual(authorize.searchParams.get('scope').split(' ').sort(), [
    'api:use-streams-read', 'api:use-datasets-read', 'api:use-sql-queries-read',
    'api:use-sql-queries-execute', 'offline_access'].sort());
  env.location.href = `${APP}?code=one-time-code&state=${pending.state}`;
  await env.auth.initAuth(CONFIG);
  assert.equal(env.location.href, original);
  assert.equal(env.exchanges.length, 1);
  const body = env.exchanges[0].options.body;
  assert.equal(body.get('code_verifier'), pending.verifier);
  assert.equal(body.get('redirect_uri'), APP);
  assert.equal(env.auth.getAuthState().authenticated, true);
  assert.equal(env.memory.has('backprop.oauth.pending'), false);
  assert.ok(env.replaced.every(url => !new URL(url).searchParams.has('code')));
});

test('OAuth rejects a foreign return URL before exchanging a code', async () => {
  const env = await browser();
  await env.auth.initAuth(CONFIG);
  await env.auth.login();
  const pending = JSON.parse(env.memory.get('backprop.oauth.pending'));
  pending.returnUrl = 'https://foreign.example/stolen';
  env.memory.set('backprop.oauth.pending', JSON.stringify(pending));
  env.location.href = `${APP}?code=one-time-code&state=${pending.state}`;
  await assert.rejects(env.auth.initAuth(CONFIG), /return address could not be verified/);
  assert.equal(env.exchanges.length, 0);
  assert.equal(new URL(env.location.href).origin, new URL(APP).origin);
});

test('OAuth rejects mismatched state and removes the one-time callback code', async () => {
  const env = await browser();
  await env.auth.initAuth(CONFIG);
  await env.auth.login();
  env.location.href = `${APP}?code=one-time-code&state=wrong-state`;
  await assert.rejects(env.auth.initAuth(CONFIG), /could not be verified/);
  assert.equal(env.exchanges.length, 0);
  assert.equal(new URL(env.location.href).searchParams.has('code'), false);
  assert.equal(env.memory.has('backprop.oauth.pending'), false);
});

test('Simultaneous expired-token reads share one refresh request', async () => {
  const env = await browser();
  const originalNow = Date.now, now = originalNow();
  env.memory.set('backprop.oauth.session', JSON.stringify({accessToken: 'expiring', refreshToken: 'refresh', expiresAt: now + 120000}));
  await env.auth.initAuth(CONFIG);
  assert.equal(env.exchanges.length, 0);
  Date.now = () => now + 180000;
  try {
    const tokens = await Promise.all([env.auth.getAccessToken(), env.auth.getAccessToken()]);
    assert.deepEqual(tokens, ['test-token', 'test-token']);
    assert.equal(env.exchanges.length, 1);
    assert.equal(env.exchanges[0].options.body.get('grant_type'), 'refresh_token');
  } finally { Date.now = originalNow; }
});

test('A refused token refresh clears both the cached and stored session', async () => {
  const env = await browser();
  env.memory.set('backprop.oauth.session', JSON.stringify({accessToken: 'expired', refreshToken: 'revoked', expiresAt: 0}));
  globalThis.fetch = async () => new Response('{}', {status: 401});
  await assert.rejects(env.auth.initAuth(CONFIG), /sign-in failed \(401\)/);
  assert.equal(env.auth.getAuthState().authenticated, false);
  assert.equal(env.memory.has('backprop.oauth.session'), false);
  await assert.rejects(env.auth.getAccessToken(), /Sign in with Foundry/);
});

test('Foundry numeric-string token lifetimes are accepted and malformed values rejected', async () => {
  for (const expiresIn of ['3600', 3600, 'not-a-number', '', -1, null, true]) {
    const env = await browser();
    env.memory.set('backprop.oauth.session', JSON.stringify({accessToken: 'expired', refreshToken: 'refresh', expiresAt: 0}));
    globalThis.fetch = async () => new Response(JSON.stringify({access_token: 'new-token', expires_in: expiresIn}),
      {status: 200, headers: {'Content-Type': 'application/json'}});
    if (expiresIn === '3600' || expiresIn === 3600) {
      await env.auth.initAuth(CONFIG);
      assert.equal(await env.auth.getAccessToken(), 'new-token');
      const saved = JSON.parse(env.memory.get('backprop.oauth.session'));
      assert.ok(saved.expiresAt > Date.now() + 3500 * 1000);
      assert.ok(saved.expiresAt < Date.now() + 3700 * 1000);
    } else {
      await assert.rejects(env.auth.initAuth(CONFIG), /invalid sign-in response/);
      assert.equal(env.auth.getAuthState().authenticated, false);
    }
  }
});

test('Run control asks extra scope only on explicit consent and preserves narrower grants', async () => {
  const env = await browser(`${APP}?project=team%2Fproject&run=run-1`);
  await env.auth.initAuth(CONFIG);
  assert.equal(env.auth.getAuthState().canControlRuns,false);
  await env.auth.login({allowRunControl:true});
  const url = new URL(env.location.assigned), pending = JSON.parse(env.memory.get('backprop.oauth.pending'));
  assert(url.searchParams.get('scope').split(' ').includes('api:use-streams-write'));
  env.location.href = `${APP}?code=one-time&state=${pending.state}`;
  await env.auth.initAuth(CONFIG);
  assert.equal(env.auth.getAuthState().canControlRuns,true);
  assert.equal(new URL(env.location.href).searchParams.get('run'),'run-1');
  await env.auth.login({allowRunControl:true});
  const next = JSON.parse(env.memory.get('backprop.oauth.pending'));
  globalThis.fetch = async () => Response.json({access_token:'read-only',expires_in:3600,scope:'api:use-streams-read'});
  env.location.href = `${APP}?code=two&state=${next.state}`;
  await env.auth.initAuth(CONFIG);
  assert.equal(env.auth.getAuthState().canControlRuns,false);
});

test('Read-only legacy sessions and foreign consent return URLs cannot enable controls', async () => {
  const env = await browser();
  env.memory.set('backprop.oauth.session',JSON.stringify({accessToken:'old',expiresAt:Date.now()+3600000}));
  await env.auth.initAuth(CONFIG);
  assert.equal(env.auth.getAuthState().canControlRuns,false);
  await assert.rejects(env.auth.login({allowRunControl:true,returnUrl:'https://other.example/'}),/return address/);
  await env.auth.login();
  assert(!new URL(env.location.assigned).searchParams.get('scope').includes('write'));
});

test('a stalled refresh clears the shared guard and stored session; sign-in can recover', async () => {
  const env=await browser();
  env.memory.set('backprop.oauth.session',JSON.stringify({accessToken:'synthetic',refreshToken:'synthetic-refresh',expiresAt:0}));
  const originalTimer=globalThis.setTimeout;
  globalThis.setTimeout=(fn,ms,...args)=>originalTimer(fn,Math.min(ms,10),...args);
  globalThis.fetch=()=>new Promise(()=>{});
  try {
    await assert.rejects(env.auth.initAuth(CONFIG),{name:'TimeoutError'});
    assert.equal(env.memory.has('backprop.oauth.session'),false);
    await assert.rejects(env.auth.getAccessToken(),/Sign in/);
    env.memory.set('backprop.oauth.session',JSON.stringify({accessToken:'recovered',expiresAt:Date.now()+3600000}));
    await env.auth.initAuth(CONFIG);
    assert.equal(await env.auth.getAccessToken(),'recovered');
  } finally {globalThis.setTimeout=originalTimer;}
});
