/**
 * Purpose: Own Google sign-in and identity for the shared dashboard.
 * Input: A public GCP connection and a Google OAuth browser client.
 * Output: Authorized tokens and the existing authentication interface.
 */
import {withDeadline} from '../network.mjs';

const CONSENT_TIMEOUT_MS = 10 * 60 * 1000;

/**
 * Purpose: Scope Google browser authentication to one installation.
 * Input: Configuration supplied through the existing adapter methods.
 * Output: The shared dashboard authentication and identity interface.
 */
export function createAdapter() {
    let config;
    let session = null;
    let pending = null;
    let tokenClient;
    const key = () => `backprop.google.session.${config.gcp_id}`;
    const clear = () => { session = null; sessionStorage.removeItem(key()); };
    const valid = () => Boolean(session?.accessToken && session.expiresAt > Date.now() + 60000);

    function getAuthState() {
        const authenticated = Boolean(session?.accessToken);
        return {authenticated, user: authenticated ? session.identity.displayName : null,
            canControlRuns: authenticated, canSaveWorkspaces: authenticated,
            message: authenticated ? 'Connected to Google Cloud' : 'Sign in with Google to view experiments'};
    }

    /**
     * Purpose: Load Google's supported browser token client once.
     * Input: The configured public OAuth client ID.
     * Output: A token client whose popup uses Google's account chooser.
     */
    async function initializeClient() {
        if (!config.client_id) throw new Error('Configure a Google OAuth browser client ID for sign-in.');
        if (!globalThis.google?.accounts?.oauth2) {
            await withDeadline(() => new Promise((resolve, reject) => {
                const script = document.createElement('script');
                script.src = 'https://accounts.google.com/gsi/client';
                script.onload = resolve;
                script.onerror = () => reject(new Error('Google sign-in could not load.'));
                document.head.append(script);
            }));
        }
        tokenClient = google.accounts.oauth2.initTokenClient({
            client_id: config.client_id, scope: 'openid email profile',
            callback: () => {}, error_callback: () => {},
        });
    }

    /**
     * Purpose: Verify Google authorization before the app can publish or control runs.
     * Input: A token response from the Google account chooser.
     * Output: A scoped browser session after the backend verifies its identity.
     */
    async function acceptToken(result, signal) {
        const expiresIn = Number(result.expires_in);
        if (result.error || !result.access_token || !Number.isFinite(expiresIn) || expiresIn <= 60) {
            throw new Error('Google sign-in failed. Please sign in again.');
        }
        const identity = await withDeadline(async signal => {
            const response = await fetch(config.host + '/api/backprop/identity', {
                signal, redirect: 'error', credentials: 'omit',
                headers: {Authorization: `Bearer ${result.access_token}`},
            });
            if (!response.ok) throw new Error('This Google account does not have access to this installation.');
            return response.json();
        }, {signal});
        if (signal.aborted) throw signal.reason;
        if (typeof identity.userId !== 'string' || !identity.userId ||
            typeof identity.displayName !== 'string' || !identity.displayName) {
            throw new Error('Google Cloud returned an invalid user identity.');
        }
        session = {accessToken: result.access_token, expiresAt: Date.now() + expiresIn * 1000, identity};
        sessionStorage.setItem(key(), JSON.stringify(session));
        return getAuthState();
    }

    /**
     * Purpose: Share one Google popup request across concurrent consumers.
     * Input: The initialized Google token client and current session.
     * Output: A verified session or an explicit timeout or consent failure.
     */
    async function requestToken() {
        if (pending) return pending;
        pending = withDeadline(signal => new Promise((resolve, reject) => {
            if (!tokenClient) { reject(new Error('Google sign-in is not ready. Retry after it loads.')); return; }
            tokenClient.callback = result => acceptToken(result, signal).then(resolve, reject);
            tokenClient.error_callback = () => reject(new Error('Google sign-in was cancelled or blocked.'));
            tokenClient.requestAccessToken({prompt: session ? '' : 'select_account'});
        }), {timeoutMs: CONSENT_TIMEOUT_MS}).catch(error => { clear(); throw error; })
            .finally(() => { pending = null; });
        return pending;
    }

    return {
        async initAuth(settings) {
            config = settings;
            try {
                session = JSON.parse(sessionStorage.getItem(key()));
                if (!session?.accessToken || !session.identity?.userId ||
                    !Number.isFinite(session.expiresAt)) clear();
            } catch { clear(); }
            if (config.client_id) await initializeClient();
            return getAuthState();
        },
        getAuthState,
        async login({returnUrl = location.href} = {}) {
            const destination = new URL(returnUrl);
            if (destination.origin !== location.origin) throw new Error('Invalid sign-in return address.');
            const state = await requestToken();
            location.assign(destination.href);
            return state;
        },
        logout() { clear(); location.reload(); },
        async getAccessToken() {
            if (session && !valid()) await requestToken();
            return valid() ? session.accessToken : null;
        },
        runControlScope: () => null,
        resolveWorkspaceIdentity: (request, options) => request('/api/backprop/identity', options),
        getArchiveOffsets(response) {
            let offsets;
            try { offsets = JSON.parse(response.headers.get('X-Backprop-End-Offsets')); }
            catch { throw new Error('Google history omitted its stream boundaries.'); }
            if (!offsets || Array.isArray(offsets) || typeof offsets !== 'object' ||
                Object.keys(offsets).length !== 16 ||
                Array.from({length: 16}, (_, index) => String(index)).some(partition =>
                    typeof offsets[partition] !== 'string' || !/^[0-9]+$/.test(offsets[partition]))) {
                throw new Error('Google history omitted its stream boundaries.');
            }
            return offsets;
        },
        validateConfig(settings) {
            const origin = new URL(settings.host);
            if (settings.backend !== 'gcp' || origin.protocol !== 'https:' ||
                origin.origin !== settings.host || origin.port || origin.username || origin.password ||
                origin.origin !== location.origin || !/^[0-9a-f]{32}$/.test(settings.gcp_id || '') ||
                settings.stream_dataset_rid !== 'ri.foundry.gcp.dataset.events' ||
                settings.media_dataset_rid !== 'ri.foundry.gcp.dataset.media' ||
                settings.stream_view_rid || (settings.branch && settings.branch !== 'master')) {
                throw new Error('Backprop deployment configuration is invalid.');
            }
        },
        requestError: status => `Google Cloud request failed (${status}). Check access or sign in again.`,
        presentation: {
            label: 'Google', connection: 'Connected to Google Cloud', canSignOut: true,
            footer: 'Google Cloud Backprop', source: 'Cloud Run + Firestore history',
            installCommand: 'python -m pip install "backprop-foundry[gcp]"',
            setupTitle: 'Configure the Google Cloud connection',
            setupDescription: 'Set BACKPROP_CONFIG to this file and configure Google Application Default Credentials.',
            environment: 'export BACKPROP_CONFIG="/path/to/backprop-config.json"\ngcloud auth application-default login',
        },
    };
}
