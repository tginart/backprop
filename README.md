# Backprop

Backprop brings W&B-style experiment tracking to Palantir Foundry, local SQLite, or Google Cloud.

It provides a familiar Python API for experiment logging, distributed training,
artifacts and experiment inspection. Backprop runs on three different backend
platforms. GCP, Palantir Foundry, and Local SQLite.

All three backends use the same Python APIs and dashboard. The [backend
READMEs](#how-it-runs) explain each platform's architecture, authentication, and operations.

![Backprop workspace comparing training and validation
metrics](assets/images/workspace.png)

Compare runs and follow their metrics in the workspace.

<details>
<summary>Explore the runs table and a single experiment</summary>

**Runs table.** Browse experiments and compare their latest results.

![Backprop runs table with experiment states and summary
metrics](assets/images/runs.png)

**Run details.** Inspect an experiment's charts, configuration, logs, files and
artifacts from one view.

![Backprop single-run view with automatically generated metric
charts](assets/images/run-details.png)

</details>

## Local backend

Install Backprop 0.1.3 in your training environment:

```bash
python -m pip install 'backprop-foundry @ git+https://github.com/tginart/backprop.git@v0.1.3'
```

For a complete demo from this release's source checkout, run
`python examples/local_smoke.py`.

The smoke demo opens the local dashboard, records live metrics, a table and a
Plotly chart, and verifies an artifact download. The dashboard stays up afterward;
press Ctrl-C to stop it. Run it again to see the saved runs and add a new one.

To host your own experiments:

```bash
backprop serve ./backprop-data
```

Open [the dashboard](http://127.0.0.1:8765). In another terminal, set
`BACKPROP_CONFIG` to the absolute path of `backprop-data/connection.json` and run
your existing training code. Local mode requires no token or browser sign-in.
The installed CLI includes the dashboard assets and needs no source checkout.

See [local setup, journal import, and backup/restore](docs/local-backend.md) and
[backend verification](docs/backend-verification.md).

## Quick Start

Requires Python 3.10 or newer. Install Backprop 0.1.3 in the environment that runs your
training code:

```bash
python -m pip install 'backprop-foundry @ git+https://github.com/tginart/backprop.git@v0.1.3'
```

The Python package contains the SDK. Deploying the browser app requires a source
checkout:

```bash
git clone --branch v0.1.3 https://github.com/tginart/backprop.git
cd backprop
```

Follow the [setup guide](docs/foundry-setup.md) to deploy Backprop in your Foundry
enrollment. Download the connection file from the app's setup page or use the generated
`backend/state.json`:

```bash
export BACKPROP_CONFIG=/path/to/backprop-config.json
```

Supply a Foundry token through `FOUNDRY_TOKEN` or `BACKPROP_TOKEN`, or call
`backprop.login()` to save one locally. The package name is `backprop-foundry`; the
Python import is `backprop`.

```python
import backprop as wandb

with wandb.init(project="my-experiment", config={"lr": 1e-4}) as run:
    run.define_metric("tokens", hidden=True)
    run.define_metric("train/*", step_metric="tokens")

    for step in range(100):
        run.log({"tokens": step * 1024, "train/loss": 1 / (step + 1)})

    run.log({"examples": wandb.Table(
        columns=["input", "prediction"], data=[["hello", "world"]]
    )})
    run.summary["result"] = "complete"
    print(run.url)
```

Open the printed URL to inspect the run. Charts are created automatically from logged
metrics; tables, media and artifacts appear alongside them.

Offline logging needs no Foundry account:

```python
import backprop

with backprop.init(project="demo", mode="offline") as run:
    run.log({"loss": 0.5})
```

For runnable comparisons, artifact pipelines, shared writers and training telemetry, see
the [examples guide](examples/README.md).

To keep existing `import wandb` statements, run your training program through the
compatibility launcher:

```bash
python -m backprop run -- training.py [arguments]
python -m backprop run -- -m your_training_module [arguments]
```

The launcher redirects W&B imports within that Python process. Setting the official
SDK's `WANDB_BASE_URL` to Backprop will not work: compatibility is at the Python API
level.

Frameworks and media formats use **optional dependency groups**, called extras. The base
install stays small; an extra installs the libraries needed for a particular capability.
From a source checkout, for example:

```bash
python -m pip install '.[media,plots]'
```

| Extra | What it adds |
| --- | --- |
| `media` | NumPy and Pillow for image/array inputs. |
| `video` | Array-to-video encoding and its supporting libraries. |
| `plots` | Plotly, Matplotlib and statistical plotting dependencies. |
| `dataframes` | pandas, Polars and Arrow for dataframe inputs and exports. |

Extras such as `torch`, `tensorboard` and `lightning` add their respective framework
integrations. Packages are never installed automatically when a feature is used. See
[`pyproject.toml`](pyproject.toml) for all groups and the [authentication
guide](docs/python-api-support.md#authentication) for credential settings. Each
framework has its own Python requirements. Legacy fastai 1 has known dependency security
limits; see [dependency security](docs/dependency-security.md).

## How it runs

Every backend uses the same event model, durable SDK journal, replay rules, and
browser views. The SDK uploads every five seconds by default; the browser refreshes
every 30 seconds. The selected adapter determines hosting, storage, and authentication.

| Backend | Application and storage | Architecture and operations |
| --- | --- | --- |
| Foundry | Hosted static website, streaming dataset with archive, and file dataset. Clients use Foundry APIs directly. | [Foundry README](backend/foundry/README.md) |
| Local SQLite | Loopback HTTP server, SQLite events and metadata, and a local artifact directory. | [Local SQLite README](backend/local/README.md) |
| Google Cloud | Cloud Run dashboard/API, transactional Firestore events and metadata, and GCS artifacts. | [Google Cloud README](backend/gcp/README.md) |

Each backend README includes its own flow diagram, connection instructions,
authentication model, operational boundaries, and code map. Start with the
[Foundry setup](docs/foundry-setup.md), [local setup](docs/local-backend.md), or
[Google Cloud deployment](docs/gcp-setup.md) guide for the complete operator workflow.

Select a connection file with `BACKPROP_CONFIG`. Configuration and summary edits,
artifact metadata, and cooperative stop requests remain events on every backend.
Media and artifact snapshots keep immutable bytes in the selected backend's file
storage. External artifact references retain their bytes at the original provider.

## W&B Python SDK Compatibility

Backprop targets **`wandb==0.30.0`** for experiment logging, distributed logging,
artifacts and experiment inspection.

| Area | Supported APIs and behavior |
| --- | --- |
| Lifecycle and metrics | `init`, `log`, `finish`, `Config`, configuration and summary updates, `define_metric`, resume, offline logging, custom axes and summary aggregates. |
| Tables, media and charts | `Table`, `data_types.PartitionedTable`, `JoinedTable`, `EvalTable`, `Image`, `Audio`, `Video`, `Html`, `Histogram`, `Graph`, `Plotly`, `wandb.plot` helpers and locally registered custom charts. |
| Files, artifacts and models | `save`, `restore`, `Artifact`, `log_artifact`, `use_artifact`, `log_model`, `use_model`, immutable snapshots, editable metadata, tags and aliases. |
| Distributed logging and telemetry | `init(mode="shared")`, `run.upsert_artifact`, `run.finish_artifact`, `watch`, `unwatch`, PyTorch parameters/gradients/observed graphs and optional system metrics. |
| Inspection and edits | `Api`, public `Run` history/file/artifact reads, project/run creation, configuration and summary edits, and cooperative stopping. |
| Integrations | TensorBoard, Keras, Lightning Fabric, XGBoost, LightGBM, CatBoost, sklearn, fastai 1, SB3 and Gym/Gymnasium capture. |

The [API support guide](docs/python-api-support.md) describes supported APIs and
options. See [compatibility and migration](docs/compatibility.md) to adapt existing
code.

### Not supported

- **Experiment orchestration:** `sweep`, `agent`, `controller`, sweep inspection,
  Launch, jobs and run queues.
- **Hosted services and administration:** automations, alerts, Model Registry
  links, W&B identity/account administration, and deletion/retention APIs.
- **Specialized formats and other integrations:** `Object3D`, `box3d`, `Molecule`,
  `data_types.Bokeh`, `data_types.WBTraceTree` and framework adapters outside the
  selection above.
- **Separate products and service machinery:** Reports, Weave, Workspaces,
  W&B-hosted chart-specification IDs, and W&B server/background-service protocols.

The [support guide](docs/python-api-support.md#not-supported) lists the exact names.
Unsupported APIs and feature options raise `NotImplementedError`, also catchable as
`backprop.UnsupportedError`. Supported no-ops such as `mode="disabled"` retain their
documented behavior.

### Foundry differences

Foundry provides credentials, permissions, URLs and storage placement. `storage_region`
is unsupported. Artifact versions are content-based rather than sequential W&B version
numbers, and sampled history can differ from W&B.

Edits and shared-writer contributions become visible as events arrive; they do not
promise immediate cross-client consistency. Shared runs have a designated primary and
preserve each writer's local steps.

Multi-device GPU capture and some Plotly configurations are experimental. TensorBoard
imports do not support in-place event-file rewrites. See the [API support
guide](docs/python-api-support.md) for runtime requirements and limitations.

## Architectural Overview

The repository separates the SDK (`sdk/backprop/`), dashboard (`web/`), and backend
adapters (`backend/foundry/`, `backend/local/`, and `backend/gcp/`). One connection configuration
selects the backend. Both clients read the same event schema to reconstruct runs,
histories and artifact relationships. Browser authentication adapters live in
`web/backends/`; shared Python connection and HTTP code lives under `backend/`.

| Component | Code | Responsibility |
| --- | --- | --- |
| Python entry points | [`__init__.py`](sdk/backprop/__init__.py), [`compatibility.py`](sdk/backprop/compatibility.py) | Run lifecycle, module-level helpers and the `wandb` import aliases used by the launcher. |
| Logging and persistence | [`run.py`](sdk/backprop/run.py), [`transport.py`](sdk/backprop/transport.py), [`connection.py`](backend/connection.py) | A run records changes in a durable journal; the selected adapter publishes acknowledged batches and files. |
| Values and adapters | [`media.py`](sdk/backprop/media.py), [`integration`](sdk/backprop/integration/__init__.py) | Tables, media, artifacts, plotting, training hooks and optional framework callbacks. These use the same run and journal rather than separate upload paths. |
| Experiment inspection | [`apis/public/api.py`](sdk/backprop/apis/public/api.py), [`replay.py`](sdk/backprop/apis/public/replay.py) | `Api` queries, returned run/file/artifact objects, event replay and public edits. |
| Browser app | [`web/app.mjs`](web/app.mjs), [`api.js`](web/api.js), [`model.mjs`](web/model.mjs) | Navigation and views, authenticated reads, and reconstruction of the run model. Chart, table and media viewers live in separate modules. |
| Backend adapters | [`foundry/store.py`](backend/foundry/store.py), [`local/server.py`](backend/local/server.py), [`gcp/database.py`](backend/gcp/database.py) | Foundry integration, local SQLite storage, or transactional Firestore events with GCS artifacts. |
| Installation tools | [`backend/provision.py`](backend/provision.py), [`deploy/deploy.py`](deploy/deploy.py), [`deploy/gcp_deploy.py`](deploy/gcp_deploy.py) | Provision and publish Foundry resources or deploy isolated Google Cloud resources. Local installations use the installed `backprop serve` CLI. |

The [documentation index](docs/README.md) links the installation, migration and
interface guides. See [contributing](CONTRIBUTING.md) for local development and test
commands.

## Support and security

See [release notes](RELEASE_NOTES.md) for compatibility and limitations, [dependency
security](docs/dependency-security.md) for known dependency risks, and the [security
policy](SECURITY.md) to report a vulnerability.

Use [project issues](https://github.com/tginart/backprop/issues) for questions and bug
reports. Keep tokens, local deployment settings and private experiment data out of
reports and commits.

## License

Backprop is licensed under [Apache-2.0](LICENSE). See [NOTICE](NOTICE) and [third-party
notices](THIRD_PARTY_NOTICES.md) for bundled components and attribution.
