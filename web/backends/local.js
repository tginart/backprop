/**
 * Purpose: Own the credential-free loopback connection policy.
 * Input: An explicit local connection at the dashboard's origin.
 * Output: Local authentication state without OAuth or token access.
 */
export function createAdapter() {
    let config;
    const getAuthState = () => ({authenticated: true, user: 'Local user',
        canControlRuns: true, canSaveWorkspaces: true, message: 'Connected to local Backprop'});
    return {
        initAuth: async settings => { config = settings; return getAuthState(); }, getAuthState,
        login: async () => getAuthState(), logout() {},
        getAccessToken: async () => null, runControlScope: () => null,
        resolveWorkspaceIdentity: async () => ({userId: config.local_id,
            displayName: 'Local user'}),
        getArchiveOffsets(response) {
            const offset = response.headers.get('X-Backprop-End-Offset');
            if (!/^[0-9]+$/.test(offset || '')) {
                throw new Error('Local history did not include its stream boundary.');
            }
            return {'0': offset};
        },
        validateConfig(settings) {
            const origin = new URL(settings.host);
            const loopback = origin.hostname === 'localhost' || origin.hostname === '[::1]' ||
                /^127(?:\.[0-9]{1,3}){3}$/.test(origin.hostname);
            if (origin.protocol !== 'http:' || !loopback || origin.origin !== settings.host ||
                origin.origin !== location.origin || origin.username || origin.password ||
                settings.stream_dataset_rid !== 'ri.foundry.local.dataset.events' ||
                settings.media_dataset_rid !== 'ri.foundry.local.dataset.media' ||
                settings.stream_view_rid || (settings.branch && settings.branch !== 'master')) {
                throw new Error('Backprop deployment configuration is invalid.');
            }
        },
        requestError(status) {
            return `Local Backprop request failed (${status}). Check the local server and retry.`;
        },
        presentation: {
            label: 'Local', connection: 'Connected locally', canSignOut: false,
            footer: 'Local Backprop', source: 'Local stream + SQLite history',
            installCommand: 'python -m pip install --force-reinstall /path/to/backprop_foundry-*.whl',
            setupTitle: 'Configure the local connection',
            setupDescription: 'Set BACKPROP_CONFIG to this file. Local connections do not require a token or sign-in.',
            environment: 'export BACKPROP_CONFIG="/path/to/backprop-config.json"',
        },
    };
}
