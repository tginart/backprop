/**
 * Purpose: Expose the shared data interface to dashboard components.
 * Input: One connection configuration and workspace requests.
 * Output: Canonical events and media from the selected backend.
 */
import {createClient} from './backends/http.js';

export {isWorkspaceEvent} from './workspace-store.mjs';
export const {initialize, getConfiguration, getWorkspaceIdentity, publishWorkspaceEvent, parseCsv, recordToEvent, loadRunControl, publishRunStop, loadWorkspace, getMediaUrl, getBackendPresentation, exportConfiguration} = createClient();
