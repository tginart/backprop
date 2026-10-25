/**
 * Purpose: Select one browser backend from the same connection configuration as the SDK.
 * Input: Public connection settings with an optional backend selector.
 * Output: Shared authentication methods and backend presentation metadata.
 */
import {createAdapter as createFoundry} from './foundry.js';
import {createAdapter as createLocal} from './local.js';

let adapter;

function select(settings) {
    const name = settings.backend === undefined ? 'foundry' : settings.backend;
    if (name === 'foundry') return createFoundry();
    if (name === 'local') return createLocal();
    throw new Error('Unknown Backprop backend');
}

export function validateConfiguration(settings) {
    select(settings).validateConfig(settings);
}

export async function initAuth(settings) {
    adapter = select(settings);
    return adapter.initAuth(settings);
}

export const getAuthState = () => adapter?.getAuthState() ||
    {authenticated: false, canControlRuns: false, user: null, message: 'Connect to Backprop'};
export const getBackendPresentation = () => adapter?.presentation ||
    {label: 'Backprop', connection: 'Connected', canSignOut: false, footer: 'Backprop'};
export const getAccessToken = () => adapter.getAccessToken();
export const login = options => adapter.login(options);
export const logout = () => adapter.logout();
export const runControlScope = () => adapter.runControlScope();
export const requestError = (...options) => adapter.requestError(...options);

export function exportConfiguration(config) {
    const keys = ['backend', 'local_id', 'host', 'stream_dataset_rid',
        'media_dataset_rid', 'stream_view_rid', 'branch', 'app_url', 'client_id',
        'stream_write_scope', 'map_resource_origins'];
    return Object.fromEntries(keys.filter(key => config?.[key] !== undefined)
        .map(key => [key, config[key]]));
}

export const resolveWorkspaceIdentity = (request, options) => adapter.resolveWorkspaceIdentity(request, options);

export const getArchiveOffsets = response => adapter.getArchiveOffsets?.(response) || null;
