# Using Backprop

A project contains experiments. Its workspace is the default view of their charts, with
a runs sidebar beside the chart area. The Runs page shows the same experiments in a full
table. These are views of one project, not separate stores.

## Find runs and compare charts

- Use the project selector and the labeled navigation rail to change views.
  Rail buttons expose their names on hover and keyboard focus.
- Eyes control which runs are plotted. Table checkboxes select records for
  actions; selection and chart visibility are independent.
- Search, filters, grouping, sorting and columns use the same run analysis in
  the sidebar and full table. Regular-expression search reports invalid input.
- Charts appear automatically from logged values, grouped by key prefix.
  Sections collapse and page six panels at a time. Scalar charts use the logged
  axis definition and the first ten matching visible runs by default.
- Hover or use keyboard inspection to see recorded values. Legends toggle
  individual series. Charts support zoom, reset, fullscreen and exports.
- Open a run name for Charts, Overview, Logs, Files and Artifacts. Returning to
  the project preserves the previous exploration state.

Run visibility, filters and other exploration preferences belong to **this browser**.
Custom panels, layout editing, named/shared workspaces, remote autosave and undo/redo
are not available in 0.1.0.

## Inspect logged values

Tables, histograms, plots and media receive automatic panels. Rich outputs are stacked
by run/writer. Tables support search, sorting, pagination, column visibility, row
inspection and CSV export. The step selector reconstructs earlier snapshots, including
incremental tables. Images retain their original colors; boxes and masks appear as
independent annotation layers. Audio and video use browser playback controls.

Plotly renders locally, including supported 3D traces. Custom charts use locally
registered specifications, not arbitrary W&B-hosted IDs. Files and artifact versions
expose authenticated downloads. Artifact versions are content-based; lineage shows
relationships recorded in the current project.

## Loading, refreshing and access

The initial screen identifies the project and shows an animated loading status. The app
polls every 30 seconds while visible. Pausing stops automatic reads; Refresh remains
available. Failed refreshes retain the last successful data and show the error. Empty
projects offer Python connection instructions.

Sign-in uses Foundry permissions. Project/entity names are namespaces, not separate
authorization boundaries within one installation. Logged HTML runs in a script-disabled
sandbox. External map resources follow the operator's origin policy; Foundry credentials
are not sent to providers. See [dependency security](dependency-security.md).

Run-state edits record state; they do not kill training. Stop controls require explicit
write consent and target a cooperating SDK process. An acknowledgment is not
confirmation that training finished.

## Grouping and workspace limits

Grouping aligns exact recorded X coordinates. It does not interpolate missing points or
reproduce W&B's server bucketing. Shared writer-local steps cannot be used as a grouping
axis. Missing contributors remain visible. The browser has a one-million-event workspace
limit. See the [API support guide](python-api-support.md) for format and runtime
requirements, and [dependency security](dependency-security.md) for renderer
constraints.
