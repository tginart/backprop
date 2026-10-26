# Backend connection contract

The SDK and dashboard use one public connection configuration. Its `backend`
field selects `foundry`, `local`, or `gcp`. Files without that field select Foundry for
compatibility. Credentials stay outside the configuration.

The SDK reads the file selected by `BACKPROP_CONFIG`. The dashboard reads the
same shape from `config.json`. Local startup writes `connection.json` for the SDK
and serves its contents at `/config.json`. Foundry website deployment generates
its public `config.json`; the dashboard can download that configuration for the SDK.
The public OAuth client ID is safe to include. SDK clients ignore browser-only fields.

Foundry example:

```json
{
    "backend": "foundry",
    "host": "https://your-foundry.example",
    "stream_dataset_rid": "ri.foundry.main.dataset.events",
    "media_dataset_rid": "ri.foundry.main.dataset.media",
    "branch": "master",
    "client_id": "your-public-oauth-client-id",
    "app_url": "https://your-dashboard.example/"
}
```

For local use, run `backprop serve ~/backprop-data` and set
`BACKPROP_CONFIG=~/backprop-data/connection.json`. That generated file selects
`local`, the bound loopback HTTP origin, and a stable installation ID. It requires
no token or OAuth. Existing documented `BACKPROP_*` environment overrides still
apply; clear an old host or resource override before you switch connection files.

## Ownership

- `connection.py`: select the Python adapter and expose shared SDK interfaces.
- `records.py`: canonical event validation and immutable-identity conflicts,
  independent of database storage.
- `http_store.py`, `http_controls.py`, and `artifact_resolutions.py`: the
  shared event, history, file, retry, and atomic resolution HTTP contract.
- `foundry/`: HTTPS and credential policy, Foundry upload contention,
  and resource setup/verification.
- `local/`: loopback policy, credential-free adapter, HTTP server, SQLite, artifacts,
  and read-only journal import.
- `gcp/`: Google credentials, Cloud Run HTTP policy, native Firestore transactions,
  partition cursors, immutable GCS artifacts, and bounded multipart uploads.
- `assets.py` and `dashboard_assets.json`: the explicit public asset boundary used
  by the installed local server and Foundry website packaging.

The SDK owns public training/analysis APIs, event models, and durable journals.
`web/` owns dashboard views and visualization models. Browser adapters live in
`web/backends/`: `connection.js` selects an adapter, `http.js` shares the data
contract, and `foundry.js` and `local.js` own browser authentication and policy.
Dashboard views and SDK consumers do not select a backend. Legacy SDK module names forward to backend
implementations so existing imports and test patch identities remain valid.

Wheels install all three adapters as `backprop_backend`, with all dashboard resources.
Use `python -m pip install -e .` for source development. A new backend adapter can
use these interfaces without a backend branch in the SDK or dashboard components.

## Foundry operator commands

```bash
python backend/foundry/provision.py --project-rid PROJECT_RID
python backend/foundry/verify.py
```

The old `backend/provision.py` and `backend/verify.py` paths forward to these
commands. Operator-selected resource state and verification output paths stay in
place for scripts that already use them.

See [local startup, imports, backups, and limitations](../docs/local-backend.md).

See [backend verification](../docs/backend-verification.md) for the test matrix and live checks.

See [Google Cloud deployment](../docs/gcp-setup.md) for configuration, authentication,
storage guarantees, deployment, verification, and cleanup.
