# Backprop examples

These demos run real, small optimizations on synthetic data. They use the public Python
SDK and your configured local or Foundry installation. They do not provision Foundry resources.

| Example | What it demonstrates | Runs online? | Extra dependencies |
| --- | --- | --- | --- |
| [local_smoke.py](local_smoke.py) | One command starts the local dashboard, live metrics, table, Plotly and verified artifact download | Local server | None beyond the SDK |
| [seed_demo.py](seed_demo.py) | Four learning rates, scalar comparisons, incremental tables, confusion matrix, probability image and model artifacts | Optional | `media` |
| [offline_sync.py](offline_sync.py) | Durable local logging, incremental tables, upload later and an empty repeated sync | Only with `--sync` | None beyond the SDK |
| [resume_training.py](resume_training.py) | Save model state, then continue the same run in a separate invocation | Required | None beyond the SDK |
| [artifact_pipeline.py](artifact_pipeline.py) | Dataset -> training -> evaluation, verified downloads, aliases, metadata edits, joined predictions and input/output lineage | Required | `media` |
| [shared_training.py](shared_training.py) | Spawn independent writers, preserve their identities and steps, and finish through one primary | Required | None beyond the SDK |
| [torch_training.py](torch_training.py) | CPU neural-network training, gradients/parameters, observed graph, system metrics and an optimizer checkpoint | Optional | `torch,media,stats` |
| [plotly_gallery.py](plotly_gallery.py) | Interactive 2D progress and 3D parameter/loss trajectories, with their source table | Optional | `plots` |
| [inspect_project.py](inspect_project.py) | Read experiment summaries and export full histories through `Api` | Required, read-only | None beyond the SDK |

## Local smoke test

From this source checkout, install the SDK and start the demo:

```sh
python -m pip install .
python examples/local_smoke.py
```

It opens the dashboard and records a one-minute optimization. Watch `train/loss`,
inspect the table and Plotly chart under `outputs`, and download `model.json` from
Artifacts. The terminal reports `SMOKE PASSED` after SDK readback succeeds.
The dashboard stays up until Ctrl-C; data and journals persist under
`.backprop/local-smoke/`. Each invocation adds a new run.

Use `--directory PATH` to choose the data location or `--port 0` for a free port.
For a fast automated run without a browser launch:

```sh
python examples/local_smoke.py --port 0 --steps 5 --interval 0.02 --no-open --exit-after-demo
```

The remaining examples use `BACKPROP_CONFIG` for either backend. Local connections
need no token. The Foundry credential instructions below apply to Foundry mode.

## Setup

Install Backprop into your training environment or Conda environment as described in the
[Quick Start](../README.md#quick-start). From a checkout, installation with all
dependencies used by these examples is:

```sh
python -m pip install '.[media,plots,torch,stats]'
```

Use that environment's `python` for the commands below. If you already have the SDK
installed, you can run these scripts from the checkout without an editable install.

For online runs, point to the SDK connection file downloaded from the app, or the
resource state created during [Foundry setup](../docs/foundry-setup.md):

```sh
export BACKPROP_CONFIG=/absolute/path/to/backprop-config.json
```

Supply your Foundry token through `FOUNDRY_TOKEN` / `BACKPROP_TOKEN`, or use
`backprop.login()`. Do not use `deploy/config.local.json` as the SDK connection file.
The remaining examples accept `--config PATH` as an alternative to `BACKPROP_CONFIG`. Examples do
not read operator secrets or require Google Cloud.

The default entity/project is `backprop/backprop-examples`. Change it with `--entity
TEAM --project PROJECT`. Each invocation creates a unique execution group and an output
directory under `.backprop/examples/`; `--output PATH` changes that parent directory.
The printed `result.json` files contain run IDs, URLs (null for offline runs) and local
journal locations, never credentials. Keep journals until uploads finish successfully.

## 1. Compare experiments

Start without a connection:

```sh
python examples/seed_demo.py --steps 60
```

Or publish the four runs directly:

```sh
python examples/seed_demo.py --mode online --steps 60
```

Open a printed URL. Return to the project Workspace, show the four runs using the eye
controls, and expand `train` and `eval`. Faster learning rates should reduce loss
sooner. In `evaluation`, inspect the prediction table, confusion matrix and probability
map. In a run's Artifacts tab, download its model JSON. All metrics come from the actual
optimization; no artificial metric curve is logged.

## 2. Record offline and upload later

```sh
python examples/offline_sync.py --steps 12
```

The script prints the exact `python -m backprop sync ...` command for its journal. After
supplying a connection and token, that command uploads its events and files. You can
also exercise both stages in one invocation:

```sh
python examples/offline_sync.py --sync
```

The second sync must leave the acknowledgement cursor unchanged. `sync()` returns the
cumulative acknowledged event count, not the number uploaded by that call. In the app,
the incremental `training/checkpoints` table should have one row per epoch, without
duplicate rows from the retry. Offline logging is distinct from model checkpoint
restoration.

## 3. Continue from a checkpoint

```sh
python examples/resume_training.py --steps 20
python examples/resume_training.py --steps 20 --checkpoint /path/printed/by/first/run/checkpoint.json
```

Use the same entity/project for both commands. The first invocation saves weights and
the next epoch in `checkpoint.json`. The second loads them and uses `resume="must"` with
the same run ID. Logging resume alone cannot restore a model.

The run should contain epochs 0 through 39 with a continuous loss curve. Reusing an old
checkpoint against a run that has advanced fails explicitly. The first segment finishes
normally; this is a controlled continuation example, not a crash-recovery guarantee.
Allow new events to become visible before restarting from another process or machine.

## 4. Follow artifact lineage

```sh
python examples/artifact_pipeline.py --steps 40
```

This creates three runs. Training downloads the producer's dataset artifact; evaluation
downloads the trained model. The evaluation run logs a JoinedTable, confusion chart and
probability image. It adds the model's `evaluated` alias and records held-out accuracy
in artifact metadata without changing model bytes.

Inspect each run's Artifacts tab and the project's Artifacts page. The dataset and model
should each have a producer and a consumer. `latest`, `training`, `candidate` and
`evaluated` are collection aliases, not sequential version numbers. The script passes
acknowledged artifact handles between stages; a fresh client's alias lookup may need to
wait for Foundry visibility.

## 5. Run independent shared writers

```sh
python examples/shared_training.py --workers 3 --steps 30
```

The parent owns the primary handle. Three spawned Python processes use the same run ID
with distinct `x_label` values. Each trains its own synthetic shard, writes its own
journal, and logs its own `train/loss` and `train/accuracy` measurements. The parent
waits for successful worker exits before saving all shard weights and finishing the run.
A worker failure or deadline fails the example visibly.

Open the shared run's charts and inspect writer contributions against `epoch`. The
primary also logs a `workers/results` table and a model artifact containing one JSON
file per worker. This is **distributed logging**, not synchronized SGD: the example does
not average gradients or pretend writer-local steps are one training process. It uses
local processes, no scheduler or cluster. Worker joins do not imply immediate visibility
to other Foundry readers.

## 6. Inspect training telemetry and interactive charts

```sh
python examples/torch_training.py --mode online --steps 80
python examples/plotly_gallery.py --mode online --steps 40
```

The PyTorch demo explicitly uses CPU. It logs parameters and gradients every five steps,
an observed model graph, predictions and a checkpoint containing model and optimizer
state. Hooks are removed on exit. System metrics use a one-second sampling interval, so
short runs may contain only the initial sample.

The Plotly demo logs a 2D loss curve and a 3D trajectory through two learned weights and
their measured loss. Expand `plots`, open a figure, hover over points and rotate the 3D
view. The `tables/trajectory` panel exposes the source values. Both demos default to
offline when `--mode online` is omitted.

## 7. Query the results

```sh
python examples/inspect_project.py
python examples/inspect_project.py --group comparison-EXECUTION_ID
```

Use the exact group from a demo's `result.json`. The read-only script writes a cross-run
`runs.csv` and one JSON history file per run. JSON preserves nested cells and
shared-writer metadata. This is intended for small demo projects: complete histories are
loaded into memory. A run can be absent or incomplete until its latest events are
visible; rerun the query after the next refresh.

See [contributing](../CONTRIBUTING.md) to run the example tests.
