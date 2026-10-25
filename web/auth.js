/**
 * Purpose: Expose the backend authentication interface to dashboard code.
 * Input: The shared connection configuration.
 * Output: Authentication state and actions from the selected adapter.
 */
export {initAuth, getAuthState, getAccessToken, login, logout, runControlScope,
    getBackendPresentation} from './backends/connection.js';
