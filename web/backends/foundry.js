/**
 * Purpose: Own Foundry origin checks, OAuth PKCE, consent, and token refresh.
 * Input: Public Foundry connection settings and browser session storage.
 * Output: Authentication and identity for the shared browser client.
 */
import {withDeadline} from '../network.mjs';

export function createAdapter() {
    let config;
    let session = null;
    let refreshing = null;
    const SESSION_KEY = 'backprop.oauth.session';
    const PENDING_KEY = 'backprop.oauth.pending';
    const READ_SCOPES = ['api:use-streams-read', 'api:use-datasets-read', 'api:use-sql-queries-read', 'api:use-sql-queries-execute', 'offline_access'];
    const WRITE_SCOPES = ['api:use-streams-write', 'api:streams-write'];

    function runControlScope() {
      const scope = config?.stream_write_scope || 'api:use-streams-write';
      if (!WRITE_SCOPES.includes(scope)) throw new Error('The configured run-control permission is invalid.');
      return scope;
    }

    function randomString() {
      return base64url(crypto.getRandomValues(new Uint8Array(48)));
    }

    function base64url(bytes) {
      return btoa(String.fromCharCode(...bytes)).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
    }

    function saveSession(value) {
      session = value;
      if (value) sessionStorage.setItem(SESSION_KEY, JSON.stringify(value));
      else sessionStorage.removeItem(SESSION_KEY);
    }

    async function exchange(parameters, requestedScopes = session?.scopes || []) {
      const result = await withDeadline(async signal => {
      const response = await fetch(`${config.host}/multipass/api/oauth2/token`, {
        signal,
        method: 'POST', credentials: 'omit', redirect: 'error',
        headers: {'Content-Type': 'application/x-www-form-urlencoded', Accept: 'application/json'},
        body: new URLSearchParams({...parameters, client_id: config.client_id}),
      });
      if (!response.ok) throw new Error(`Foundry sign-in failed (${response.status}). Please sign in again.`);
      return response.json();
      });
      // Foundry's API reference permits a numeric string here; OAuth libraries may
      // return a JSON number. Validate both representations before doing arithmetic.
      const expiresIn = ['number', 'string'].includes(typeof result.expires_in) ? Number(result.expires_in) : NaN;
      if (typeof result.access_token !== 'string' || !result.access_token || !Number.isFinite(expiresIn) || expiresIn <= 0 ||
          !Number.isFinite(Date.now() + expiresIn * 1000)) throw new Error('Foundry returned an invalid sign-in response.');
      if (result.scope !== undefined && typeof result.scope !== 'string') throw new Error('Foundry returned invalid authorization scopes.');
      // OAuth permits scope to be omitted when the granted scopes match the request.
      // Explicitly narrower responses always win; an old session never gains write
      // permission merely because this version of the app supports stop controls.
      const scopes = result.scope === undefined ? requestedScopes : result.scope.split(/\s+/).filter(Boolean);
      saveSession({accessToken: result.access_token, refreshToken: result.refresh_token || session?.refreshToken,
        expiresAt: Date.now() + expiresIn * 1000, scopes});
    }

    /** Validate the OAuth callback before exchanging its one-time authorization code.
     * Main callers: api.initialize. The URL is cleaned even when authorization fails.
     */
    async function initAuth(settings) {
      config = settings;
      const saved = sessionStorage.getItem(SESSION_KEY);
      if (saved) {
        try { session = JSON.parse(saved); }
        catch { saveSession(null); }
      }
      const url = new URL(location.href);
      if (url.searchParams.has('code') || url.searchParams.has('error')) {
        const pendingRaw = sessionStorage.getItem(PENDING_KEY);
        sessionStorage.removeItem(PENDING_KEY);
        const code = url.searchParams.get('code');
        const state = url.searchParams.get('state');
        const error = url.searchParams.get('error');
        for (const key of ['code', 'state', 'error', 'error_description']) url.searchParams.delete(key);
        history.replaceState({}, '', url);
        if (error) throw new Error('Foundry sign-in was cancelled or denied.');
        const pending = pendingRaw ? JSON.parse(pendingRaw) : null;
        if (!pending || pending.state !== state || Date.now() - pending.createdAt > 600000) {
          throw new Error('This sign-in request expired or could not be verified. Please sign in again.');
        }
        let returnUrl;
        try { returnUrl = new URL(pending.returnUrl || pending.redirectUri); }
        catch { throw new Error('The sign-in return address could not be verified. Please sign in again.'); }
        if (returnUrl.origin !== location.origin || returnUrl.origin !== new URL(config.app_url || `${location.origin}/`).origin) {
          throw new Error('The sign-in return address could not be verified. Please sign in again.');
        }
        await exchange({grant_type: 'authorization_code', code, redirect_uri: pending.redirectUri, code_verifier: pending.verifier},
          Array.isArray(pending.scopes) ? pending.scopes : READ_SCOPES);
        // The registered OAuth callback stays fixed; the selected run's local URL
        // is restored only after state, origin, and the code exchange are verified.
        history.replaceState({}, '', returnUrl);
      }
      if (session) await getAccessToken();
      return getAuthState();
    }

    function getAuthState() {
      const canPublish = Boolean(session?.accessToken && Array.isArray(session.scopes) && session.scopes.includes(runControlScope()));
      return {authenticated: Boolean(session?.accessToken), user: session ? 'Foundry user' : null,
        canControlRuns: canPublish, canSaveWorkspaces: canPublish,
        message: session ? 'Connected to Foundry' : 'Sign in with Foundry to view experiments'};
    }

    /** Begin the normal Foundry consent flow with a cryptographic one-use verifier.
     * Main callers: sign-in, enable-run-controls and enable-workspace-saving buttons.
     * Consent only enables the existing event stream write scope; the caller still
     * performs its separate stop or workspace save after returning to the app.
     */
    async function login({allowRunControl = false, allowWorkspaceSave = false, returnUrl = location.href} = {}) {
      if (!config) throw new Error('Application configuration has not loaded.');
      const verifier = randomString();
      const state = randomString();
      const redirectUri = config.app_url || `${location.origin}/`;
      const destination = new URL(returnUrl);
      if (destination.origin !== location.origin) throw new Error('Invalid sign-in return address.');
      const scopes = [...READ_SCOPES, ...(allowRunControl || allowWorkspaceSave ? [runControlScope()] : [])];
      const challenge = base64url(new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(verifier))));
      sessionStorage.setItem(PENDING_KEY, JSON.stringify({state, verifier, redirectUri,
        returnUrl: destination.href, scopes, createdAt: Date.now()}));
      const url = new URL(`${config.host}/multipass/api/oauth2/authorize`);
      url.search = new URLSearchParams({client_id: config.client_id, response_type: 'code', redirect_uri: redirectUri,
        scope: scopes.join(' '),
        state, code_challenge: challenge, code_challenge_method: 'S256'});
      location.assign(url);
    }

    function logout() {
      saveSession(null);
      sessionStorage.removeItem(PENDING_KEY);
      location.reload();
    }

    /** Refresh once when needed; simultaneous API calls share that one request.
     * Main callers: the Foundry API adapter. Failures require explicit sign-in.
     */
    async function getAccessToken() {
      if (!session?.accessToken) throw new Error('Sign in with Foundry to continue.');
      if (Date.now() >= session.expiresAt - 60000) {
        if (!session.refreshToken) { saveSession(null); throw new Error('Your Foundry session expired. Please sign in again.'); }
        if (!refreshing) refreshing = exchange({grant_type: 'refresh_token', refresh_token: session.refreshToken})
          .catch(error => { saveSession(null); throw error; }).finally(() => { refreshing = null; });
        await refreshing;
      }
      return session.accessToken;
    }

    async function resolveWorkspaceIdentity(request, {signal} = {}) {
        const user = await request('/multipass/api/me', {signal});
        if (typeof user?.id !== 'string' || !user.id.trim() || user.id.length > 512 ||
                typeof user.username !== 'string' || !user.username.trim()) {
            throw new Error('Foundry returned an invalid workspace user identity.');
        }
        return {userId: user.id, displayName: user.username};
    }

    function validateConfig(settings) {
        const origin = new URL(settings.host);
        const resourceId = /^ri\.foundry\.[a-zA-Z0-9-]+\.dataset\.[a-zA-Z0-9-]+$/;
        if (origin.protocol !== 'https:' || origin.origin !== settings.host ||
            origin.username || origin.password || !settings.client_id ||
            !resourceId.test(settings.stream_dataset_rid) ||
            !resourceId.test(settings.media_dataset_rid)) {
            throw new Error('Backprop deployment configuration is invalid.');
        }
    }

    function requestError(status, controlWrite, workspaceWrite, path) {
        return status === 401 ? 'Your Foundry session expired. Sign in again.' :
            status === 403 ? workspaceWrite
                ? 'Foundry denied workspace saving. This account needs write access to the Backprop event stream.'
                : controlWrite
                    ? 'Foundry denied the stop request. This account needs write access to the Backprop event stream.'
                    : path === '/multipass/api/me'
                        ? 'Foundry denied access to your workspace identity. Sign in again.'
                        : 'Foundry denied access. This account needs read access to the Backprop project.' :
            `Foundry request failed (${status}). Retry when the service is available.`;
    }

    return {initAuth, getAuthState, login, logout, getAccessToken, runControlScope,
        validateConfig, requestError, resolveWorkspaceIdentity, presentation: {
        label: 'Foundry', connection: 'Connected to Foundry', canSignOut: true,
        footer: 'Built on Foundry', source: 'Foundry stream + durable archive',
        installCommand: "python -m pip install 'backprop-foundry @ git+https://github.com/tginart/backprop.git@v0.1.3'",
        setupTitle: 'Configure your Foundry connection',
        setupDescription: 'Save the connection file and keep your token in the environment.',
        environment: 'export BACKPROP_CONFIG="/path/to/backprop-config.json"\nexport FOUNDRY_TOKEN="<your Foundry token>"',
    }};
}
