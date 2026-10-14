/**
 * Release boundary: automatic experiment exploration ships first; custom panel
 * layouts and shared workspace editing remain a separate, unreleased layer.
 * Main callers: app.mjs and panel-bank.mjs. This is a build-time decision, not a
 * URL option. Defaults never request stream-write access or publish UI events.
 *
 * Browsing preferences use their own key so older custom layouts are preserved
 * without changing the default chart bank or implying an account-level save.
 */
export const CUSTOM_WORKSPACES = false;
export const EXPLORATION_STORAGE_KEY = CUSTOM_WORKSPACES
  ? 'backprop.workspace.v1' : 'backprop.exploration.v1';
