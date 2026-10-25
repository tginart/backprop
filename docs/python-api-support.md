# Python API support

This guide describes Backprop **0.1.2**: experiment logging, distributed logging,
artifacts and experiment inspection through the W&B **0.30.0** Python API. It lists
supported names, Foundry differences and runtime requirements.

Here, `wandb` means `import backprop as wandb`, or W&B imports redirected by `python -m
backprop run`. The official W&B SDK cannot connect to Backprop by changing its server
URL.

Unsupported APIs fail explicitly with `backprop.UnsupportedFeatureError`. Catch it as
Python's `NotImplementedError` or the SDK's `UnsupportedError`/`UsageError`. The
compatibility launcher shares these classes through `wandb.errors`. Missing modules fail
at import; missing members fail on lookup or call. For example, `wandb.sweep(...)`,
`artifact.is_link`, `run.alert(...)` and `api.runs(include_sweeps=True)` raise.
Unsupported settings such as `Settings(launch=True)` fail before mutation or run
creation. Disabling an unavailable service is valid. Normal supported no-ops, including
disabled logging and upstream-deprecated options, retain their documented behavior.

## Supported

`run` in this table is the logging **`wandb.Run` returned by `wandb.init()`**.

| Python names | What works |
| --- | --- |
| `wandb.init()`, `setup()`, `teardown()`, `finish()`, `join()`; `run.finish()` | Run lifecycle, context managers, reinitialization, resume, fork/rewind, offline and disabled logging. |
| `wandb.log(data, step=None, commit=None)`, `run.log(...)` | Metrics and rich values, explicit steps, and merging calls into an open row before committing. |
| `wandb.define_metric(...)`, `run.define_metric(...)` | Custom step axes, axis carry-forward, prefix globs and summary aggregates. |
| `wandb.Config`, `wandb.config`, `run.config`; `wandb.summary`, `run.summary` | Configuration and summary reads/updates, including supported nested mutations. Summary's class import is `wandb.sdk.wandb_summary.Summary`. |
| `wandb.save()`, `restore()`, `log_artifact()`, `use_artifact()`, `log_model()`, `use_model()`; corresponding `run` methods; `run.log_code()` | File saving/restoration, immutable artifacts and model files, source and notebook capture. |
| `wandb.pin_config_keys()`, `mark_preempting()`; `run.pin_config_keys()`, `mark_preempting()`, `status()`, `get_url()`, `to_html()`, `display()` | Local configuration pinning, preemption state, upload status and run links/display. |
| `wandb.login()`, `wandb.Settings` | Foundry authentication and supported logging settings, subject to the restrictions below. |
| `wandb.Table`, `Histogram`, `Image`, `Audio`, `Video`, `Html`, `Plotly`, `Classes` | Common data types, image boxes/masks/classes, and table dtype/relationship metadata. |
| `Table.add_data()`, `add_row()`, `add_column()`, `add_computed_columns()`, `cast()`, `set_pk()`, `set_fk()`, `get_column()`, `get_index()`, `index_ref()`, `get_dataframe()`, `iterrows()` | Table construction, mutation and retrieval; immutable, mutable and incremental logging modes. |
| `wandb.plot.line()`, `line_series()`, `scatter()`, `bar()`, `histogram()`, `confusion_matrix()`, `roc_curve()`, `pr_curve()` | Built-in charts and locally registered custom specifications. |
| `wandb.Artifact`; `Artifact.add()`, `add_file()`, `add_dir()`, `add_reference()`, `new_file()`, `remove()`, `finalize()` | Construct an artifact draft and its manifest. `remove()` edits the draft, not a stored artifact. |
| `Artifact.get()`, `get_entry()`, `get_path()`, `files()`, `file()`, `download()`, `checkout()`, `verify()`, `wait()` | Read, download and verify immutable snapshots. |
| `Artifact.new_draft()`, `save()` | Create new snapshots, publish pending drafts through the latest active or an automatic run, and save descriptive edits on committed artifacts. |
| `Artifact.collection()`, `logged_by()`, `used_by()` | Read collection and run relationships. |

These **read API** methods operate on results stored in Foundry:

| Python names | What works |
| --- | --- |
| `wandb.Api().projects()`, `.project()`, `.runs()`, `.run()`, `.from_path()`, `.flush()` | Query experiments, filter/sort runs, resolve supported paths and refresh cached reads. |
| `Api.artifact()`, `artifacts()`, `artifact_versions()`, `artifact_exists()` | Read immutable artifacts and their versions. |
| `Api.artifact_collections()`, `artifact_collection()`, `artifact_collection_exists()`, `artifact_types()`, `artifact_type()` | Read artifact collections and types. |
| `wandb.apis.public.Run.history()`, `scan_history()`, `beta_scan_history()`, `download_history_exports()`; `Runs.histories()` | Sampled/full history and optional dataframe/Parquet exports. |
| Public `Run.files()`, `file()`, `logged_artifacts()`, `used_artifacts()`, `console_logs()` | Logged file, artifact and console reads; returned files support `download()`. |
| Public `Run.load()`, `load_full_data()`, `wait_until_finished()` | Reload snapshots and wait for a recorded finish event. |
| Public `Run.config`, `rawconfig`, `summary`, `summary_metrics`; `Project.artifacts_types()`, `collections()`; `ArtifactType.collections()`, `collection()` | Read configuration/summaries and traverse artifact types and collections. `artifacts_types` is the upstream spelling. |

`Artifact.save(project=..., settings=...)` publishes a pending artifact through the
newest active run. If none is active, it creates and finishes a run with
`job_type="auto"`, applying the supplied project and Settings. Without explicit Settings
the automatic run is silent. Queued offline objects remain pending until upload
acknowledgment. Once committed, `save()` edits the artifact in its existing project;
project/settings arguments do not start another run. Foundry credentials and delayed
visibility apply to these automatic runs too.

Artifact publication assigns `latest` even when explicit aliases are supplied. Committed
artifacts expose an editable alias list; call `save()` after edits. Empty aliases are
valid and appear as `""` in the UI. Explicit names such as `v1` are user aliases, while
immutable versions remain content-based. Immutable version lookup takes precedence over
an alias with the same spelling. Edits become visible as Foundry events arrive.

`use_artifact()` waits for a new draft's publication before recording it as an input. A
pending object owned by another online run keeps its original identity and owner. Its
requested aliases publish before the input record. `type` checks name-based lookups; as
in W&B, object inputs ignore it. Deprecated `use_as` warns and has no effect. Explicit
offline calls raise `TypeError`.

Using an offline-owned artifact uploads just its frozen manifest and files. The
producer's other data stays local. The transfer preserves its original namespace, run
owner, content version and queued aliases; a later full producer sync converges on the
same identity. W&B may publish that pending object using only the consumer's intent.
Failed transfers report a durable journal that can be retried with `backprop sync` after
the process exits.

Offline incremental artifacts have a provisional content version until their base is
selected. Sync freezes the full manifest and rewrites queued config references to its
final version. It leaves the original journal unchanged. Restored local handles observe
this cache; downloading inherited Foundry files still requires an online store. Base
selection uses currently visible events. A first-write record in the existing file
dataset makes all copied journals use the same choice, including when their stream views
differ. Newly arriving versions do not alter that choice.

Re-logging a pending Artifact returns the same Python object and moves its local
publication owner, project and producer together. A cross-project output gets a stable
destination artifact ID derived from its source snapshot and destination; repeated
copies converge on that ID. Previously captured journals and config references stay in
their original scope. Once committed, online publication rejects moves to another entity
or project. An offline copy can publish there, but the committed source object keeps its
original ownership and editable baseline. Aliases belong to each destination collection;
a source alias is not automatically copied. This replaces W&B's background save-handle
ownership with an explicit durable journal owner.

Cloud reference publication propagates provider authentication and metadata failures
before publishing an artifact. HTTP references reject unsuccessful responses and use
W&B's retry policy for transient responses. Each request has a 60-second timeout; after
exhausted retries the caller can retry the operation. A failed reference download leaves
an existing destination file unchanged. Provider credentials remain local and are not
attached to Foundry events.

Offline config capture is a native extension: assigning an Artifact to a top-level
config key queues its files and reference for later sync, while W&B 0.30.0 rejects that
assignment.

**The two `Run` classes serve different stages.** `init()` returns a live logging
handle. `Api.run()` returns an editable experiment snapshot: metadata/config need
`update()` or `save()`, while summary assignments publish immediately.

`use_model()` records an artifact input, checks for `model` in its type without regard
to letter case, and downloads its contents. If the download contains one file or
directory, that item is returned; otherwise the download root is returned. Offline calls
raise `RuntimeError`. The run's artifact view shows its recorded inputs separately from
outputs. The native global helper works across repeated and disabled sessions; W&B
0.30.0 can leave its global helper bound to an old run.

## Additional supported APIs

| Python names | Behavior and requirements |
| --- | --- |
| `Api.create_project`, `create_run`; Public `Run.create`, `update`, `save`, `update_state`, `upload_file`, `log_artifact`, `use_artifact` | Implemented through events in existing Foundry resources. State edits do not stop a process. |
| `Artifact.aliases`, `.metadata`, `.description`, `.tags`, `.save`; `ArtifactCollection.save` | Editable overlays on immutable contents. Collection saves edit name, type, description and tags while preserving identity, files and content versions. |
| `init(mode="shared")`; `Settings.x_label`, `x_primary`, `x_update_finish_state` | Independent journals, writer IDs and local steps. Explicit primary role; eventual convergence. |
| `Run.upsert_artifact`, `finish_artifact` | Visible contribution union, conflict rejection and immutable finalization. Shared closure files remember consumed parts across delayed queries and restarts. Concurrent finalizers use the chosen manifest or create a disjoint next version. Late parts stay pending for the next close; the caller coordinates completeness. Wait for the primary to finalize before reading the combined files. |
| `Artifact.distributed_id`, `Artifact.incremental` | Local descriptors match W&B value retention, assignment/deletion behavior and lifecycle rules. Changing `distributed_id` on a committed handle does not change the stored group. |
| `Artifact(incremental=True)` through `log_artifact` | Inherits the visible previous version, with new files replacing matching paths. Offline deltas select and freeze the base at sync or online handoff; retry keeps that choice. Copies in separate journals share one immutable base-selection file in the existing Foundry dataset. An uncommitted output can move to another project; it selects that destination collection's base. A committed source copied offline keeps its original ownership. |
| `watch`, `unwatch`; logging `Run.watch`, `unwatch` | PyTorch parameters, gradients and `log_graph=True`; captured module shapes and parameters appear in the data view. Public methods return `None`. Supports repeated observations, CPU multi-rank DDP and single-GPU wrappers. Multi-device GPU capture is experimental. |
| `Graph` files and artifact reconstruction | Explicit binding freezes the current graph; rebinding republishes the first owner's path. Logging captures each observation independently. Artifact reads restore topology and duplicate IDs resolve to the last node. The viewer preserves non-finite attributes. Graph conversion depends on framework version. Keras 3 functional topology conversion raises an error, as in W&B 0.30.0. |
| `init(monitor_gym=True)`, `gym.monitor`, `integration.gym.monitor` | Completed Gym/Gymnasium recordings use ordinary video logging. Close recorders before finishing the run. The `gym` extra supplies Gymnasium 1.0.x and its video encoder. |
| Public `Run.system_metrics`; stats settings | Local psutil and optional NVIDIA collectors. Hardware availability determines measurements. |
| Public `Run.stop` | While the experiment is running or pending, requests every connected writer visible in the current snapshot. It retries durable requests and leaves completion to the writers. A writer that becomes visible later needs another call. |
| `data_types.PartitionedTable`, `JoinedTable`, `EvalTable` | Artifact-backed parts in saved manifest order; full outer joins include duplicate and unmatched keys. Evaluations preserve raw cells until publication, freeze their first published rows/roles, and use a Foundry artifact ID as `evaluate_call_id`. Binding creates no media file. SDK and browser reconstruction use the same rows and part metadata. Constructors preserve W&B input precedence and typed cells. Explicit serialization also works from disabled and finished runs without restarting experiment logging. Audio file checks and video decoding happen before publication; evaluation videos require the optional video dependencies. Support is limited to the documented constructors and serialization operations. |
| `Image`, `data_types.Image` | Public file binding preserves naming, copy/link options and annotation files. `to_json` returns W&B-shaped descriptors; native logging retains the existing Foundry format. Immutable artifact references stay fixed; latest references resolve the latest visible alias. Public separated-image serialization requires Pillow; encoded-byte file binding does not. |
| `Classes`, `data_types.Classes` | Retains caller-owned label sequences; logging and artifact writes freeze snapshots. Standalone serialization is `classes.to_json(None)`. Inherited file methods follow the W&B Media lifecycle; an ordinary class set has no file to bind. |
| `Histogram`, `data_types.Histogram` | `to_json()` returns counts and bin edges. `from_json(json_obj, source_artifact)` decodes those inline values and validates them through the constructor. This is a native extension over W&B 0.30.0's abstract decoder. Registry dispatch and artifact tables retain working histogram round trips. |
| `helper.parse_config`, `sdk.wandb_helper.parse_config` | Dicts, Namespace objects, framework flags, versioned YAML and include/exclude filters follow the W&B helper. Native init additionally accepts absent config and pathlib paths. |
| `ensure_configured`, `Settings.anonymous` | Deprecated W&B behavior: a terminal warning and no authentication effect. `ensure_configured()` returns `None`; `anonymous` is excluded from settings serialization. |
| `data_types.Media`, `BatchableMedia`, `WBValue` | Caption-only base constructors, explicit file binding and local subclass registration. User serializers can extend supported value families; artifact inputs still follow the supported family list. This does not cover arbitrary upstream subclasses or every inherited serialization method. |
| `tensorboard.patch`, `unpatch`; `init(sync_tensorboard=True)` / `tensorboard=True`; `Api.sync_tensorboard` | Supported event-file summaries and durable source identities. SDK/browser reads deduplicate independent imports without dropping writer completion records. Conflicting copies and unsupported plugins raise errors. Consecutive records at one training step merge by source order, including revisions and rewind recovery. Repeated source discovery stays deduplicated; W&B 0.30.0 can start duplicate watchers. Provider credentials and IAM permissions must be configured separately. Unpatching retains announced sources until their run finishes. Re-announcing the same live source with a different root or save policy raises `UnsupportedFeatureError`; finish its owner before changing that policy. |
| `integration.lightning.fabric.WandbLogger` | Lazy initialization, media/checkpoints and native spawned-process restoration. Workers retain one experiment ID with separate journals; the parent finishes the experiment. |
| Selected `integration.keras`, `lightning.fabric`, `xgboost`, `lightgbm`, `catboost`, `sklearn`, `fastai`, `sb3` | Selected framework callbacks; see this guide and `pyproject.toml` for runtime requirements. |
| `Plotly`, `plot_table`, `plot.plot_table`, `visualize`, `Api.create_custom_chart` | Local Plotly including 3D and locally registered Vega specifications. External W&B-hosted spec IDs stay omitted. |

See [distributed logging](distributed-logging.md) for shared-writer conflicts and
finalization.

`plot.Visualize(table, spec)` and `plot.viz.VisualizeSpec(name, key="")` are ordinary
mutable dataclasses, matching the W&B value API. Construct them before a run, retain the
same table/spec objects, and use `set_key()` to update the spec. Logging freezes the
current table and resolves its chart definition through the receiving run's Foundry
store. Nested chart keys use dotted paths; failed row validation leaves keys and table
cursors unchanged.

Built-in presets and locally registered definitions render locally. An arbitrary name
can be stored in the value, but logging an unknown definition raises an explicit lookup
error. Existing W&B-hosted chart IDs remain outside the supported rendering subset.
Rendering metadata lives in the native logged value; it is not added to the public
dataclass fields.

## Not supported

| Python surface | Unsupported behavior |
| --- | --- |
| `sweep`, `agent`, `controller`; `Api.sweep`, `Project.sweeps`, `Sweep`, `Sweeps`, `include_sweeps=True` | Search scheduling, execution and sweep metadata. |
| `sdk.launch.*`, jobs/run queues; `automations.*` | Execution services and persistent automation. |
| `alert`, `Run.alert`, `AlertLevel` | External notifications. |
| Registry APIs; artifact `link` / `unlink`; run `link_artifact` / `link_model` | Registry relationships. |
| Users, teams, organizations and W&B identity | Foundry owns permissions and authentication. |
| Run/file/artifact/collection deletion, artifact `ttl`, collection `change_type` | Destructive administration and retention. |
| `Object3D`, `box3d`, `Molecule`, `data_types.Bokeh`, `data_types.WBTraceTree` | Specialized formats and viewers. Model graphs and Plotly 3D have their own viewers. |
| Diffusers, DSPy, Hugging Face autolog, KFP, Metaflow, OpenAI fine-tuning, Prodigy, Sacred, SageMaker, legacy TensorFlow hooks, Ultralytics | Unselected integration contracts. Explicit video logging works. |
| `attach`, Reports, Slack/webhooks, `weave`, `wandb_workspaces` | Background-process attachment and separate hosted products. |
| GraphQL/protobuf/service internals, generated validators, debugger integration | W&B implementation machinery. |

Foundry tokens and URLs replace W&B credentials and URLs. Content versions replace
sequential artifact numbers. Sampling and eventual read visibility differ. Custom charts
require local registration; arbitrary existing W&B chart IDs cannot be resolved. The
restrictions below are part of the public support contract.

## Authentication

`login()`, `init()` and `Api()` use Foundry tokens. An explicit login/API key or per-run
`Settings(api_key=...)` wins. `init()` otherwise reuses a matching setup/login session
until teardown; `relogin=True` bypasses that reuse. Changing the environment does not
replace an active matching session.

Without session reuse, credential selection uses `BACKPROP_TOKEN`, then `FOUNDRY_TOKEN`,
then a matching host entry in the credential file, then a host-matched settings key.
`login()` and `Api()` use this selection directly. Credentials from another host are not
reused. W&B API keys and identity-token federation remain outside this contract.

`Settings.credentials_file` selects the cache, with
`~/.config/backprop/credentials.json` as the default. `WANDB_CREDENTIALS_FILE` selects
the same setting for redirected applications. Explicit or prompted `login()` tokens are
saved after validation and optional verification; tokens supplied through the
environment or per-run settings are not automatically saved. Cache writes use a process
lock and atomic replacement. POSIX files are created with mode 0600; insecure existing
files and malformed foreign formats raise errors. W&B credential files are never
implicitly read or overwritten.

`relogin=True` skips reusable credentials and requests a fresh token. An explicit key
still wins. `force=True` removes the offline choice. `prompt=False` returns False when
no permitted credential is available, without changing the session. Offline and disabled
runs do not read credentials or show prompts. Explicit login keys without a configured
Foundry host are usable in the current process, but cannot be cached by host; this
produces a warning.

Interactive token input is hidden. Selecting offline returns False and makes subsequent
initialization offline. `timeout` or `Settings.login_timeout` bounds each prompt attempt
and verification: prompt expiry selects disabled mode; verification expiry raises
`TimeoutError` and preserves the previous session. `referrer` appears as an encoded
query parameter in the displayed Foundry URL. Foundry owns account creation; the prompt
does not create W&B accounts. Direct inspection through `Api` requests an existing
Foundry token and cannot select a local mode. Its prompt deadline raises `TimeoutError`;
EOF or cancellation raises `KeyboardInterrupt`.

Notebook login uses the running kernel's input channel, with password-marked token
requests. A timeout leaves no background input thread and does not change the caller's
signal handlers. This does not depend on the kernel connection filename. Legacy Windows
console hosts and relogin after partially typed input are experimental.

## Annotation helpers

`wandb.data_types.BoundingBoxes2D` and `ImageMask`, including their imports under
`wandb.sdk.data_types.helper_types`, follow the W&B constructors, validators, file
binding and owner-specific serialization. Multi-device GPU capture is experimental.

Path-backed masks preserve supplied class labels, including when loaded from a file.
Binding stores labels through existing Foundry configuration events. Each update
replaces only its named layer, retaining other writers' layers; the existing event order
resolves edits to the same layer. Reads converge after the normal stream delay. Native
artifact round trips retain both the pixels and label maps. The public `to_data_array()`
hook still raises `NotImplementedError`, matching W&B for these two classes.

Mask overlays use the browser's `ImageDecoder` to read exact class IDs. This avoids
canvas readback changing class values. Browsers without that decoder show an explicit
mask error and keep the base image visible. Mask rendering requires a browser that
implements this decoder.

## HTML and Plotly files

`Html` and `Plotly` use the shared media file lifecycle for explicit `bind_to_run()`,
`file_is_set()`, `is_bound()` and `to_json(owner)` calls. Binding honors names, steps,
IDs, copying/linking and `ignore_copy_err`; source HTML files with `inject=False` are
copied, while generated temporary files move into a run.

HTML injection uses local CSS. Calling `inject_head()` later changes `.html` without
changing its already frozen file, matching the W&B lifecycle. Native UTF-8 byte inputs
remain accepted. Plotly file serialization uses W&B 0.30.0's Pydantic-first JSON
conversion. Its `from_json(json_obj, source_artifact)` reads stored JSON. Native history
and table cells retain the existing inline plot specification and viewer. Encoding and
browser rendering are separate contracts; encoding a figure does not guarantee that
every trace or external provider renders.

The browser renders layout frames with persistent selections, templates and baseframes,
including sliders and repeated updates to the same chart. Animation timing can differ
from the standalone Plotly viewer. Concurrent updates and authenticated external
providers are experimental.

## Audio and Video files

`Audio` and `Video` use source files and the shared Media binding rules. The classes are
available through the root, `data_types` and concrete module imports, with the same file
and batch serialization behavior. Video descriptors include encoded dimensions.
`Audio.sample_rates()` returns the supplied rate, and `durations()` returns a duration
only for raw samples; detected file metadata remains available to the native viewer.
`Audio.resolve_ref()` returns the original provider URI, or `None` for an artifact-owned
byte file.

The public decoders retain two W&B decoding conventions: Audio requires a `caption` key,
and Video ignores descriptor captions/dimensions. Native `Artifact.get()` retains
recorded display metadata. Audio batching uses the standard library and does not require
W&B's optional SoundFile dependency.

With `allow_media_symlink=True`, generated temporary media is copied into the journal so
a later sync survives the encoder process exiting. Caller-owned files keep the existing
link behavior. Re-encoding a Video creates a new binding while earlier logged rows
retain their immutable bytes.

## Table file binding and artifact joins

`Table.bind_to_run()` writes a real `.table.json` file and honors the inherited
filename, run, key, step, ID and copy options. Rebinding snapshots current rows;
INCREMENTAL files start after the last committed logging cursor. The legacy file uses
`Table.MAX_ROWS`; native artifacts keep their existing richer row/schema representation
and separate artifact limit. Binding itself does not advance a logging cursor.

`JoinedTable` accepts table objects, partitioned tables, artifact table paths and
manifest entries. Foundry entry copies include the referenced table's media and schema
dependencies, so the destination remains readable independently. Browser joins use the
same key comparisons as Python for date, datetime, tuple and frozenset keys, including
microsecond timestamps and equivalent time zones. Duplicate keys retain every matching
pair; unmatched rows remain visible. Mutable containers fail explicitly. Joined tables
remain artifact values: direct run-file binding raises `ValueError`.


## Public Table JSON

`Table.to_json(run_or_artifact)` and `Table.from_json(json_obj, source_artifact)` use
W&B argument names and owner checks. Explicit artifact JSON contains W&B's
typed-dictionary schema; ordinary Backprop logging retains its existing durable snapshot
format. Both formats preserve evaluation roles and remain readable through
`Artifact.get()`.

Public serialization stages NumPy array columns as real NPZ files, converts timestamps
to milliseconds, and records relationship dependencies. The browser reads these files
from the enclosing immutable manifest and displays dates, arrays, linked rows and media.
Failed reads can retry after visibility catches up. Native incremental snapshots still
use their explicit Foundry artifact chain; public incremental files use the
manifest-local ordering.

Custom-value and custom-type callbacks use the containing file and its validation rules.
The NPZ browser viewer does not support structured or extended-precision dtypes; it
reports an error rather than displaying null values. It limits decoding to 256 MiB, 4
million array nodes and 32 dimensions. Python rejects object/pickle NPZ data, as W&B
0.30.0 does.


Media extensions that call `Media.to_json()` or `BatchableMedia.to_json()` reuse the
source artifact entry when re-serializing a downloaded value. Backprop retains an integrity-checked
byte snapshot and the entry's recorded origin; it does not require a separate W&B
reference resolver. Source/latest links and bound run paths remain available to callers.
URI-backed Image and Audio values retain provider links.

`WBValue` uses object-identity equality and is unhashable, as in W&B. Its base
`to_data_array()` raises `NotImplementedError`; individual media classes supply
their own conversions. `Image.to_data_array()`
provides a concrete conversion. Filename helpers follow W&B's string-input validation
instead of coercing arbitrary values.


## Framework integrations

Keras checkpoint callbacks skip publication when a best-only save correctly produces no
new file. Artifact storage failures propagate so training code can retry using the
retained local checkpoint. `PathLike` inputs are normalized before reaching Keras. Keras
still chooses when and what to save; Backprop publishes the resulting files through its
existing journal.


The sklearn helpers support cluster models and confusion-matrix label filters,
including rectangular selections. Confusion matrices also keep actual and
predicted labels on the correct axes; W&B 0.30.0 swaps those labels. Native
imports keep using Backprop even when the official W&B package was imported first.

The sklearn controls support micro/macro curves, feature titles and limits, cluster
names, binary calibration selection and timing-panel visibility. See [chart
controls](compatibility.md#sklearn-chart-controls) for intentional differences from
options ignored by W&B.


## Artifact logging and shared writers

Artifact output logging defaults path inputs to type `unspecified`; model logging uses
`model`. Both accept files, directories and supported reference URIs. Existing artifact
objects retain immutable files and versions when logged again; new output relationships
and descriptive changes use ordinary journal events. Disabled calls return their inert
result before checking arguments. Module helpers remain usable across later runs,
including disabled sessions.


Shared artifact retries distinguish logical writers. A nonempty `Settings.x_label` lets
a restarted handle find its own pending contribution. With no label or an empty label,
each handle owns a distinct journal and generated writer ID; recover an interrupted
process explicitly with `backprop.sync(path)`.

`Artifact.wait()` raises `ArtifactNotLoggedError` for an unlogged or unacknowledged
offline artifact. Online wait deadlines raise `WaitTimeoutError`, available at
`wandb.sdk.artifacts.exceptions`; it can also be caught as the SDK's `Error` or Python's
`TimeoutError`. A committed artifact returns immediately, including when a timeout
argument would be invalid for a pending upload.

For `run.upsert_artifact(a)`, `a.wait()` confirms acceptance of that contribution and
can return the same object with `state == "PENDING"`. Call it again after the primary
uses `finish_artifact()`: once that completion is visible, the handle becomes
`COMMITTED` and exposes the combined files. Each handle follows its own contribution, so
a late part cannot attach itself to an earlier completed version. Offline contribution
handles still require explicit sync and an authenticated reader. Independent runs
currently need an explicit `log_artifact(a)` to record the completed union as their
output.


## Inspection and edits

`Api()` verifies Foundry read credentials during construction. Explicit keys win;
otherwise a successful login or earlier implicit API session keeps its host-scoped token
ahead of later environment changes. Failed verification leaves the previous session
unchanged. A different host requires its own credentials. `Api` ignores the
initialization setting `relogin=True`, matching W&B's inspection behavior. An empty
explicit key uses the implicit credential path. Successful implicit authentication
updates the process defaults; an explicit key stays local to that API handle. A zero or
negative request timeout raises `AuthenticationError` before starting network work. W&B
can publish implicit credentials before its final authentication check. Backprop commits
credentials only after that check succeeds, preserving the previous session and cache on
failure. API-only proxy overrides stay local to the reader. Overrides are retained in
`Api.settings`, including custom metadata and an organization default. Storing a default
does not enable organization or registry operations; those calls still raise
`UnsupportedFeatureError` before contacting Foundry. Only the `http` and `https` proxy
entries configure the transport.

Inspection `Run.system_metrics` returns the latest recorded values without downloading
external files. Its returned dictionary is detached from the retained snapshot, unlike
W&B's mutable cached mapping. This read API is separate from which GPU measurements a
training process can collect. Run refreshes validate the complete replacement before
discarding previous data or unsaved local edits.

`load()` and `load_full_data()` return complete, detached snapshots with a stable
Foundry `projectId`. Cached reads preserve unsaved edits; a successful forced refresh
replaces them. Failed refreshes retain the previous snapshot. A lazy handle refreshes on
every explicit `load_full_data()` call, matching W&B. Backprop loads the full visible event snapshot together.

`Run.stop()` sends one request per observed running or pending writer, including
secondary writers in a shared run. It retries a failed journal before deciding whether a
request was already sent. Acknowledgment means that the callback ran; the primary
controls the experiment's final state. A process missing from the current snapshot needs
a later call, and the browser's stop dialog reports the primary process's request.
