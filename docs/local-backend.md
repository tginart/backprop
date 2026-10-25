# Local Backprop

Run the dashboard and SDK storage API on one machine, with SQLite for events and
an artifact directory for immutable files. Existing Foundry connection files
continue to select Foundry.

## Code layout

`backend/local/` owns the local server, SQLite storage, importer, loopback policy,
and credential-free adapter. `backend/foundry/` owns the Python Foundry adapter,
credentials, resource setup, and verification. Browser authentication adapters
live in `web/backends/`. Shared Python HTTP and connection interfaces,
plus the dashboard asset manifest, live directly under `backend/`.

The SDK retains event models and shared durable journals. Dashboard components
use the shared data and authentication interfaces. Both clients use one connection
configuration whose `backend` field selects `foundry` or `local`. Existing Foundry
files without that field remain compatible. See the [backend contract](../backend/README.md).

The distribution installs both adapters as `backprop_backend` alongside `backprop`.
Use `python -m pip install -e .` for development from a checkout. Legacy SDK module
imports and Foundry command paths remain compatible. A source checkout also
supports `PYTHONPATH=sdk` without an editable installation; its package bridge
loads the shared implementation from `backend/`.

## Install and start

Install the Backprop wheel in your training environment. A source checkout also
works with `python -m pip install -e .`. Wheels and source distributions include
the dashboard; the installed CLI needs no checkout, Node.js, or database service.
Before a release, build the feature wheel from this checkout with
`python -m pip wheel . --no-deps --wheel-dir dist`. Copy that wheel to the training
machine and install it with `python -m pip install --force-reinstall /path/to/backprop_foundry-*.whl`.
The existing public `v0.1.1` release predates the local backend.

```bash
backprop serve ~/backprop-data
```

Open [the dashboard](http://127.0.0.1:8765). The default bind address is
`127.0.0.1:8765`. Use `--port 9000` for another port, `--port 0` for a free port,
or `--host ::1` for IPv6 loopback. Public bind addresses are refused.

The data directory contains `backprop.sqlite3`, immutable bytes in `artifacts/`,
and a credential-free `connection.json`. SQLite holds events, stable offsets,
file references, history snapshots, atomic artifact-resolution claims, and local
journal import cursors. Stop with Ctrl-C. Restart against the same directory to
retain the installation identity and all stored data.

## Configure the SDK

For a one-command demo after installation, run `python examples/local_smoke.py`
from this checkout. It starts the server, opens the browser, and verifies SDK
metrics and an artifact download. See the [example guide](../examples/README.md#local-smoke-test).

```bash
export BACKPROP_CONFIG="$HOME/backprop-data/connection.json"
python train.py
```

Existing training code remains compatible:

```python
import backprop as wandb

with wandb.init(project="local-experiment") as run:
    for step in range(100):
        run.log({"train/loss": 1 / (step + 1)}, step=step)

api = wandb.Api()
```

Local connections need no token or browser OAuth. Existing `FOUNDRY_TOKEN` and
`BACKPROP_TOKEN` variables are ignored by the local transport and never sent.
`backprop.login()` works without a prompt or credential-file change.
The `login(timeout=...)`, `Api(timeout=...)`, and `init_timeout` deadlines include
the local connection handshake. Event and file retries retain their existing policy.

Local acknowledgements and incremental/distributed artifact resolution caches use
the installation identity. A resumed journal uploads its files to the selected
installation and preserves the original Foundry acknowledgement and cache files.

The connection explicitly selects `"backend": "local"` and a loopback HTTP
origin. HTTP alone never selects local mode. Foundry connections still require
HTTPS, their dataset configuration, and credentials. The browser setup page can
download a local connection file. To return to Foundry, select the original
Foundry configuration through `BACKPROP_CONFIG`.

## Import existing journals

```bash
backprop import ~/backprop-data /path/to/wandb
backprop serve ~/backprop-data
```

Pass a run directory, an `events.jsonl` file, or a parent directory. Import at
startup with `backprop serve ~/backprop-data --import-journals /path/to/wandb`.
The import command can also run while a server uses the data directory. Existing
`backprop sync RUN_DIRECTORY` uses this import path when local mode is selected.

Imports include records already acknowledged by Foundry. They preserve original
records, `ack.json`, branch state, and artifact-resolution caches. An isolated
snapshot goes through the existing SDK uploader. SQLite tracks local ingestion
separately. Native local runs also use an installation-specific acknowledgement
file instead of the Foundry acknowledgement file.

Repeat an import to ingest new records. Identical IDs do not add records or consume
offsets. A changed imported prefix or a conflicting duplicate fails explicitly.
An incomplete final line waits for the next import. After an interruption, repeat
the import; committed events remain durable and retries retain their IDs.

Keep staged `files/`, `media/`, and `live-media/` bytes available. The importer does
not fetch missing bytes from Foundry; remote-only artifacts require their original
backend. Import an offline branch's parent first. Linked media must remain
readable until the import finishes.

## SSH access

Run the server on the data host, then forward its loopback port:

```bash
ssh -N -L 8765:127.0.0.1:8765 training-host
```

Open the same dashboard URL locally. With a different client port, download the
connection file from that browser URL, or change `host` and `app_url` in a copy.
Preserve `local_id`.

## Backup and restore

Flush or finish training writers, stop the server, then copy the complete data
directory:

```bash
cp -a ~/backprop-data ~/backprop-data-backup
```

Restore that directory and start `backprop serve` against it. Use the resulting
`connection.json`. Keep the SQLite database, `artifacts/`, and any `-wal` or `-shm`
files present in the stopped directory. Copying only the database omits artifact
bytes. A plain file copy of the live database is not a supported backup.

Browser preferences stay in that browser's local storage at the same origin.
They are not part of the database backup. SDK journals remain independently
recoverable and can be imported again.

## Guarantees and limitations

Uploads succeed after a SQLite transaction commits with FULL durability. Artifact
bytes and their directory entry are flushed before their reference commits. A
lost response can leave a committed upload; replay deduplicates it. Conflicting
IDs reject the whole batch. Restart does not reset offsets. Atomic APPEND claims
retain the first artifact resolution across writers and retries. Artifact uploads
and downloads retain integrity checks.

The server supports only Backprop's stream operations, generated CSV history
queries, immutable file operations, and a local import ledger. It rejects general
SQL and unrelated Foundry APIs. There is one stream partition and one `master`
branch. Uploads are limited to 512 MiB per file and the existing 900 KB per event.
The browser retains its one-million-event workspace limit.

Use a local filesystem with reliable locks and fsync, rather than a network
filesystem. There is no multi-user access control or TLS service. Loopback and
same-origin browser checks constrain access; use SSH for remote access.

Metrics, history, resume, tables, Plotly, artifacts, workspace preferences, and
cooperative stops use the existing models. Stop requests target one advertised
process instance and invoke its stop callback; they do not kill arbitrary
processes. Named/shared workspaces keep their existing availability limits.
External map providers are blocked by the local server's default resource
policy. External artifact references still require their provider credentials.
There is no automatic event retention or artifact garbage collection.

Local archive queries retain their immutable snapshot for one hour, including
across a server restart. Canceled queries release their metadata immediately.
Expired query metadata is reclaimed on startup and when another query begins;
event and artifact records remain durable. The database schema upgrade preserves
version-one snapshots for one retry window. Local SDK reads use the complete
SQLite archive. The browser starts its live tail at that archive snapshot's
exact stream offset, including after a forced refresh.
