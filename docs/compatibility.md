# Compatibility and migration

Backprop targets experiment logging, distributed logging, artifacts and experiment
inspection against **W&B 0.30.0**. Sweeps and experiment orchestration are explicitly
omitted. This is Python API compatibility; an unchanged official SDK cannot connect by
changing `WANDB_BASE_URL`.

The [API support guide](python-api-support.md) describes supported behavior and
limitations. The [distributed logging guide](distributed-logging.md) explains
multi-writer rules.

## Use existing logging code

Install Backprop as described in the [Quick Start](../README.md#quick-start). Use
`import backprop as wandb` in your code, or keep existing `import wandb` statements and
run the program through the compatibility launcher:

```bash
export BACKPROP_CONFIG=/path/to/backprop-config.json
# Supply FOUNDRY_TOKEN through your normal secret-injection mechanism.
python -m backprop run -- training.py [existing arguments]
```

The installed distribution excludes the top-level `wandb` shim to avoid replacing an
official installation globally. The launcher shares one set of classes and active-run
globals across supported `wandb`, `wandb.sdk`, `wandb.plot`, data-type and Public Api
import paths. Unsupported framework integrations do not fall through to the W&B service.

`login()`, `init()` and `Api()` use Foundry tokens. Interactive login can prompt for a
token; explicit and prompted credentials are saved in a host-scoped private cache after
validation and optional verification. `prompt=False` stays headless. Environment and
per-run tokens are not automatically saved. See the current [authentication
contract](python-api-support.md#authentication) for precedence, relogin, timeouts and
offline choices.

## Logging and lifecycle

`init`, `setup`, `finish`, contexts and reinit modes coordinate the active run. Config
and Summary expose dictionary/attribute operations; supported nested mutations publish
updates. Name, notes, tags and group changes persist as metadata updates. Metric
definitions provide custom axes, prefix globs, axis carry-forward and aggregate
summaries.

`log(data)` commits by default. Supplying `step` leaves the row open by default;
`commit=True` closes it. Calls at the same open step merge. A call below the current
step warns and drops that call, matching W&B 0.30.0. Validation happens before changing
the pending row or a Table's incremental cursor.

Online resume supports `must`, `allow`, `never` and `auto`. Automatic resume uses a
persisted local pointer. Omitted user metadata remains intact. Fork/rewind uses an
inclusive step cutoff. An offline branch can name a remote parent without any network
access: its durable branch intent resolves on explicit sync, freezes the parent snapshot
and retains stable event IDs across retries. Later rewinds rebuild summary aggregates
from retained rows. Offline runs keep recoverable journals; disabled mode performs no
logging filesystem or network writes.

Disabled `init()` always creates a fresh global handle, regardless of `reinit`, and
skips automatic TensorBoard and Gym hooks. For ordinary runs, requested integrations are
prepared before `finish_previous` closes the existing run; failed setup leaves that run
usable. Explicit `init()` arguments update a supplied `Settings` object, matching W&B
0.30.0. `mode="run"` and `"dryrun"` retain their spelling in Settings while using online
and offline logging respectively. Backprop applies `allow_val_change` from `init()` to
config validation and the supplied Settings. W&B 0.30.0 ignores that argument.

Console capture supports Python streams, named logging handlers and native file
descriptors. Emulated terminal output preserves colors, styles, Unicode and cursor edits
in both the app and downloaded logs. Saved files use local symlinks or hardlinks so
edits through returned `save()` paths are observed; each upload is an immutable
snapshot. If linking is unavailable, live save explicitly warns and becomes a one-time
copied snapshot. Notebook capture refreshes saved source after executed cells and at
finish, and saves current-session execution history with text/display outputs. Capture
limits and failures produce explicit warnings. Code capture does not automatically
enable framework hooks or system metrics.

`force=True` works with configured Foundry credentials and offline/disabled logging;
missing online credentials still produce an explicit error. `label_disable` controls
`@wandbcode` markers in the script docstring or first notebook cell, independently of
saving code. Each initialized process supplies fresh labels, including on resume. Labels
persist in ordinary run metadata and are available through Public Api's `rawconfig`.

```python
import backprop as wandb

with wandb.init(project="experiment", config={"lr": 1e-4}) as run:
    run.define_metric("tokens", hidden=True)
    run.define_metric("train/*", step_metric="tokens")
    run.define_metric("validation/loss", summary="min,mean,last")
    run.log({"tokens": 1024, "train/loss": 0.8})
    run.summary["result"] = "complete"
```

## Data and visualization

The implementation accepts scalar/nested values, lossless nonfinite numbers, Tables and
Histograms. Tables support atomic mutation, NumPy/DataFrame inputs,
immutable/mutable/incremental snapshots, and primary/foreign-key metadata. Structural
dtype declarations use W&B's Python type engine, including evolving lists, dictionaries,
arrays and unions. Tuple/set/frozenset cells retain their Python container identity on
artifact and Public Api readback. Use `allow_mixed_types=True` for unconstrained
columns; the Python `object` class is a dtype declaration, not an alias for that option.
Registered custom dtype handlers validate locally and must be registered explicitly
before stored schemas can be reconstructed. Custom types from an already imported
official SDK retain that SDK's own validation and structural types, including optional
and nested declarations. They require registration in that existing SDK's TypeRegistry;
conflicting registered names fail explicitly. Logged data never imports a handler
module. Images support files, bytes, PIL/NumPy/tensor inputs, boxes, masks and classes.
Audio accepts files or PCM arrays; video accepts encoded files and array inputs with the
appropriate encoder. Image/Audio storage URIs can be retained inside artifacts and
Tables. Like W&B 0.30.0, direct `run.log(Image(uri))` and `run.log(Audio(uri))` reject
this reference form. HTML and Plotly data can be logged.

Built-in `plot` helpers cover line, line-series, scatter, bar, histogram, confusion
matrix, ROC and precision-recall. The browser offers run comparison, per-panel chart
controls, table exploration, media inspection and immutable files.

Automatic Histogram binning uses NumPy, and ROC/precision-recall charts use NumPy,
pandas and scikit-learn, matching the W&B dependencies and numerical behavior.
Precomputed histogram counts and edges remain dependency free.

Optional packages are imported only for the requested capability. Package extras are
`media`, `video`, `plots`, `dataframes`, `config`, `query`, `checksums`, `references`,
`aws`, `gcp` and `azure`. No package is installed automatically. Full RE2 filters
require `google-re2`; simple portable patterns work without it. Missing requested
encoders/dataframe packages fail explicitly. Like W&B, `history(pandas=True)` warns and
returns rows if pandas is unavailable.

HTML is sandboxed with scripts disabled and a separate policy blocking external network
resources. Plotly figures use a pinned official Plotly.js renderer served by Foundry,
including heatmaps, horizontal/stacked bars, date axes and subplots. Saved
colors/templates remain authoritative; otherwise the viewer supplies dark defaults. Zoom
and trace visibility survive refreshes of the same figure. Two-dimensional geographic
maps use locally hosted topology files. Modern map traces use Plotly 4.1.1, while legacy
Mapbox figures use the 3.6.0 renderer because Plotly 4 removed those trace types. Plotly
3D traces use the same local renderer. Model graphs have a separate viewer; Object3D
remains unsupported.

Logging captures finite HTTP(S) image, GeoJSON and map-style inputs into the existing
Foundry file dataset. The original figure specification stays unchanged, and
retries/retrieval reuse the captured bytes. Resource reads use no Foundry credentials.
Capture is bounded to 32 MiB per resource, 128 MiB per figure and 256 resource fields;
failures occur before publishing the pending log row. Dynamic map tiles, glyphs and
sprites remain provider requests so pan and zoom work beyond the original viewport.
Operators configure permitted provider origins in `deploy/state.json` using
`map_resource_origins`; redeployment applies matching browser and hosting policies.
Provider access tokens and CORS remain provider requirements. Blocked or unavailable
resources produce visible errors. The original figure JSON remains available.

TensorBoard imports carry a versioned source identity. SDK queries and browser views
collapse duplicate imports of that source observation, including imports from
independent writers before either sees the other's events. All writer events remain
available for completion checks. Conflicting content for one source identity raises an
error. This is eventual read-time convergence. Consecutive source records at one
training step merge in source order, including revisions and rewind recovery. Configure
provider credentials and IAM permissions separately. A live source cannot change its
root or save policy until its owner finishes.

`allow_media_symlink=True` keeps a local link for standalone path-backed media. Edits
before its first upload are captured; that chosen snapshot is persisted before network
activity and reused across retries/restarts. Reusing a scalar media object retains its
binding, while a homogeneous media batch rebinds it. Disabling the setting captures
bytes when logged. Media inside Tables and artifacts captures immutable bytes with the
containing value, except explicitly URI-backed Image/Audio references, which retain
provider metadata.

Logging a Table also produces an immutable artifact, visible through
`run.logged_artifacts()` and the app's Files tab. Built-in chart tables do the same.
Table data and its artifact manifest commit in the same native event; failed calls do
not publish orphan outputs. MUTABLE tables retain each changed snapshot, and INCREMENTAL
tables retain their previous-increment references. Bare pandas DataFrames are converted
to Tables before logging.

`Settings(allow_offline_artifacts=False)` uses the W&B-compatible offline table view and
suppresses pending artifact references. Explicit artifact logging still works. A capped
view preserves its full artifact snapshot for retrieval and UI inspection; previously
committed artifact references remain usable.

The workspace charts nested dictionaries and retains their rich-value history. Dots
separate path components; escaped dots distinguish literal dotted keys. Live refresh
preserves existing media playback and annotation controls. Run detail dialogs show when
updates are paused and provide an explicit refresh. Audio/video decoding failures
display a diagnostic and original-file download.

The Files table explorer supports search, sorting, column selection, CSV/JSON export and
navigation through saved foreign-key and row-index relationships. Relationships resolve
within the exact logged snapshot or immutable manifest.

Native Table casts return the refined WBType objects, including media, class-ID and
relationship types. Nullable relationships, nested schemas and atomic failed mutations
follow W&B type algorithms. Table dictionary keys follow sorted JSON conversion, while
resumed writers retain their original Python key constraints. Mixed incomparable
dictionary keys fail as in W&B artifact JSON.

## Artifacts and experiment queries

`Artifact` snapshots file/directory bytes when added, tracks metadata and checksums,
logs an immutable manifest, and retrieves files and supported typed objects. Content
versions include metadata and file identities. New drafts copy a previous version before
modification. MD5 entries are the default; XXH128 entries require optional `xxhash`,
with SHA-256 integrity retained for file transport. Native artifact references copy
their existing immutable content. External `file://`, HTTP(S), S3, GCS and Azure
references retain provider metadata and a source URI without uploading their file bytes.
`checksum=True` records the provider's checksum/version metadata; `checksum=False`
follows W&B provider-specific URI-tracking behavior. An explicit download uses
that provider's existing credential chain and version/integrity checks. A deleted or
changed external object may therefore fail retrieval; `add_file` creates a durable
Foundry snapshot when that is needed. Unknown schemes can be tracked with a name but
fail explicit downloads. Entry birth IDs and source reference URIs retain native
provenance across copies, drafts and readback. Public artifact IDs appear after commit,
including acknowledgement by a separate offline sync. Automatic download/checkout paths
reject unsafe collection names before changing files.

New artifact names follow W&B 0.30.0's constructor validation. Existing stored
collection names still undergo safe local-path validation when downloaded. Metadata uses
the W&B descriptive JSON conversion, including dates, enums, dataclasses and small
arrays. Dictionaries containing `_type` remain ordinary user data. Replacing
`artifact.metadata` validates immediately; in-place edits validate when saved.
`finalize()` freezes files while leaving descriptions and metadata editable. Saved
descriptive edits preserve the original file version. `storage_region` is unsupported:
the Foundry file dataset determines storage placement. Passing a non-`None` value raises
an error.

Versions use `sha256-...` identities instead of W&B's global `vN` counters. Native
version identities include captured metadata and sizes; W&B manifest checksums remain
available through `artifact.manifest.digest()` and follow the W&B path/checksum
algorithm. These are distinct identities. Use `qualified_name`, immutable ID or
`name:alias` for retrieval. Aliases and descriptive edits are recorded separately from
immutable files. Registry and destructive administrative operations remain omitted.

Provider credential chains and Settings access keys stay in the Python process
performing the download. User-supplied reference URIs remain logged data, including any
query parameters. The browser displays external entries as references with SDK retrieval
guidance.

```python
import backprop as wandb

api = wandb.Api()
runs = api.runs("team/experiment", filters={"config.lr": {"$lt": 0.01}})
for run in runs:
    rows = list(run.scan_history(keys=["train/loss"]))
    print(run.id, run.config, dict(run.summary), rows[-1:] if rows else [])
```

`Api` reads projects, runs, config, summaries, histories, console logs, files and
immutable artifacts. `history()` samples deterministic rows; `scan_history()` returns
every selected row. Metadata snapshots can be refreshed with `run.load(force=True)` or
new objects after `api.flush()`. History reads refresh automatically. Public run
creation, metadata/config edits, summary updates, state changes, file uploads and
artifact relationships persist as events. Metadata/config edits need `update()` or
`save()`; summary assignments publish immediately. Administration and destructive
operations fail explicitly.

`history_keys` is a dictionary with `lastStep` and a `keys` mapping, whose entries
include the last logged `previousValue`. Nested metrics use their dotted history names.
Counts and observed types are native extra metadata; unknown server index fields are not
invented. `load()` and `load_full_data()` expose source-named `config`, `rawconfig`,
`summaryMetrics`, `systemMetrics`, `historyKeys` and `historyLineCount` fields. Lazy
runs refresh on `load_full_data()`; full-mode runs reuse their snapshot unless
`force=True`. Clearing the API cache does not invalidate an already returned run
collection.

`Runs.histories()` with `format="pandas"` or `format="polars"` creates a dataframe for
each nonempty run before concatenation, matching W&B 0.30.0's dtype inference and
provider schema rules. Pandas resets the combined row index; Polars uses vertical
concatenation and therefore retains its schema compatibility errors. Both sort the
output columns. Incompatible Polars schemas raise the provider's error; empty runs
contribute no dataframe.

Read API methods that normalize communication errors preserve the W&B call boundary:
ordinary exceptions become `CommError`, with the original error in `.exc`; existing SDK
`Error` objects pass through. `WANDB_DEBUG=true` exposes ordinary exceptions directly.
Errors raised while subsequently iterating a returned collection remain outside the call
wrapper. Error classes retain W&B error inheritance, `.message`, `.context` and `.exc`
behavior.

As in W&B, `Api.artifact_exists()` and `artifact_collection_exists()` return `False` for
normalized validation failures and response-backed failures as well as absent objects. A
missing Foundry response or HTTP 408/504 timeout propagates. Use `artifact()` or
`artifact_collection()` when you need the actual failure; `False` alone does not prove
absence. Debug mode exposes raw validation failures.

Native Parquet exports use optional PyArrow. Mixed-type columns are preserved as JSON
cells identified by `backprop.json_columns` schema metadata. Columns with typed Table
containers or arrays also use this representation to retain tuple, set and frozenset
identity and array dtype/shape. Ordinary scalar columns remain native Parquet columns.
The completion flag and rows use one event snapshot, so resuming a run during export
cannot introduce live rows into a result marked complete. Foundry URLs, identifiers,
authentication and storage representations intentionally differ from W&B. Raw
GraphQL/protobuf objects are not emulated.

## Transport and recovery

`BACKPROP_CONFIG` contains the Foundry origin, dataset IDs, branch and app URL. Tokens
come from `BACKPROP_TOKEN`, `FOUNDRY_TOKEN` or an explicit supported credential
argument, and are excluded from persisted settings/events/metadata. Logical projects
share the installation's Foundry dataset permissions.

Events are durably appended before upload. Uploads run every five seconds by default.
Transient failures retain the journal, warn and retry with bounded backoff, including
when `stop_on_fatal_error=True`. Permanent failures stop the background sender; that
opt-in setting also invokes the training stop callback. Default mode continues local
logging. Explicit `flush()` can retry after the cause is fixed. Initialization/final
upload use configured deadlines, and no operation acknowledges unsent rows. Appending
new logs does not wait for a slow network upload.

`run.dir` contains saved run files. **`run.sync_dir` is the journal directory**:

```python
run = wandb.init(project="offline-example", mode="offline")
run.log({"loss": 0.1})
run.finish()
print(run.sync_dir)  # python -m backprop sync THIS_DIRECTORY
```

Local runs use `root_dir/wandb`, or `root_dir/.wandb` when that directory already
exists. `Settings(use_dot_wandb=True/False)` chooses explicitly. Native project/run
journals live beneath that workspace; use the returned paths rather than assuming W&B's
internal directory names. `debug.log`, `debug-internal.log` and optional latest-run
links provide local diagnostics. Older Backprop queues retain their existing paths and
can still be synchronized by passing their journal directory.

Large typed values move to authenticated JSON files after 128 KiB; events retain
references. Reads verify file integrity before publishing local downloads. Python
archive reads paginate and merge with fresh stream data using stable event IDs. Reads
reconstruct matching events in memory. Very large experiment fleets may need additional
indexing.

## Explicit boundaries

Excluded products: sweeps, agents/controllers, job orchestration, persistent
automations, alerts, registry/account administration, destructive management,
Object3D/Molecule, unselected framework integrations, arbitrary existing W&B-hosted
chart IDs and W&B's server protocol. Reports/Workspaces and Weave are separate products.
Shared logging, local telemetry, aliases and selected integrations are supported;
remaining differences are stated in the API support guide.


## sklearn chart controls

Both `wandb.sklearn` and `wandb.integration.sklearn` use the same local numerical
implementation and publish tables and chart definitions through Foundry. Backprop
implements several arguments that W&B 0.30.0 accepts but ignores:

- `plot_roc` supports micro and macro averages; `plot_precision_recall` supports
  a micro average. Averages include all observed classes. `classes_to_plot`
  selects the individual curves, so an empty selection can show only averages.
  Oversized tables share their row budget between requested curves.
- `plot_feature_importances` applies `title` and `max_num_features`. `None`
  keeps every feature; zero produces an empty chart. Features retain rank order.
- `plot_silhouette` and `plot_clusterer` apply cluster display names to both
  panels. Sequences index covered integer IDs or follow sorted observed IDs;
  mappings use the original IDs. Renamed KMeans labels require aligned fitted
  assignments to identify their centers.
- `plot_elbow_curve(show_cluster_time=False)` hides the timing panel while
  retaining measured durations in its table.
- `plot_classifier(is_binary=True)` requires two target classes and includes
  calibration diagnostics. False leaves those diagnostics out.

These are deliberate behavioral improvements, not exact reproductions of the upstream
no-ops. Historical chart snapshots keep their original specifications.


## Artifact JSON references

`Artifact.json_encode()` returns the W&B `artifactVersion` reference schema. Its `id` is
the Foundry-backed artifact ID and `version` is a content-based version. `_version:
"v0"` identifies the JSON schema, not a sequential artifact version. The dictionary can
be supplied as a top-level run configuration value to resolve the artifact through
Backprop.

Both `json_encode()` and `commit_hash` require acknowledged publication. Draft,
finalized-but-unpublished and queued offline artifacts raise `ArtifactNotLoggedError`,
an `AttributeError` subclass with `name` and `obj`. Sync or wait for publication before
reading these fields. `commit_hash` uses the immutable content digest as Foundry's
commit identity; retries and descriptive edits preserve it. Equal immutable contents can
therefore share a commit hash.
