# Backend verification

Run from a source checkout after `python -m pip install -e .`. Use Python 3.12,
Node 24 or later, and Playwright 1.62.1 with Chromium. The existing optional
training integration jobs continue to cover their installed framework versions.

## One-command verification

```bash
python tools/verify_backends.py
```

The command writes a fresh directory under `test-results/backend-verification/`.
It records runtime versions, a source fingerprint, phase logs, JSON acceptance
reports, a wheel, and a source distribution. A failed phase exits nonzero and
leaves its evidence. Use `--output DIRECTORY` to choose an empty output directory.

To include real Foundry storage:

```bash
export FOUNDRY_TOKEN='...'
python tools/verify_backends.py --foundry-config /path/to/foundry-connection.json
```

Alternatively, `--deployment-credentials` reads `FOUNDRY_TOKEN` with gcloud.
Set `BACKPROP_TEST_GCLOUD_PROJECT` to select a project or use gcloud's default.
Set `BACKPROP_TEST_GCLOUD_SECRET` to select a different secret. Credentials remain outside reports and configuration files.
The test does not provision resources, deploy a website, modify an existing run,
or stop another process. It appends uniquely named verification projects/runs and
small immutable test artifacts to the configured Backprop resources. Test records
remain identifiable by `backprop-verification-*` and
`backprop-browser-verification-*` project names.

Set `PLAYWRIGHT_MODULE` to an installed Playwright module if it is outside normal
Node resolution. `CHROMIUM_EXECUTABLE` optionally selects an existing Chromium
executable. The CI backend job installs its pinned browser runtime automatically.

## Coverage and evidence

| Gate | Local | Foundry |
| --- | --- | --- |
| Shared SDK config, legacy config, credential isolation | Real adapter and fixtures | Real adapter and fixtures |
| Event schemas, sparse offsets, retries, truncated bodies, gzip integrity | HTTP integration | HTTP contract fixtures and live acceptance |
| Large snapshots and bounded concurrent prefetch | Shared contract tests | Regression tests and real existing stream |
| Live metrics, history, resume, tables, Plotly, artifacts | Real SDK and SQLite | Real SDK and native resources when requested |
| Lost committed response and identical replay | Real SDK/server fault injection | Real HTTP publication with client response-loss fixture |
| Conflicting duplicate rejection, interrupted request, writer/reader concurrency | Real HTTP and SQLite | Existing Foundry contract fixtures; no destructive live conflicts |
| Atomic artifact resolution claims | Real storage | Real native file claims when requested |
| Offline journal sync and source preservation | Real journal/import | Real journal sync when requested |
| Imported Foundry acknowledgements and branch/incremental state | Read-only source import tests | Source state stays unchanged during local import |
| Abrupt server death, identity, offsets, installed CLI | Real process kill/restart | Native service remains managed by Foundry |
| Stopped backup and restore | Real directory copy and restart | Existing provider operations |
| Installed browser: metrics, table, Plotly, downloads, preferences, stop | Real local HTTP | Fixture plus optional real Foundry data |
| OAuth PKCE, callback validation, refresh, scopes, sign-out | No OAuth or token | Protocol fixtures |
| Wheel and source-distribution asset/import closure | Installed outside checkout | Both adapters included; Foundry website ZIP also checked |

`summary.json` distinguishes real Foundry phases from an unrequested live check.
A passing fixture run does not establish live Foundry availability.

## Focused acceptance commands

```bash
python tests/backend_acceptance.py --backend local --report /tmp/local.json
python tests/backend_acceptance.py --backend foundry \
    --foundry-config /path/to/foundry-connection.json --report /tmp/foundry.json
```

Both run the same public SDK scenario and verify an independent SQL archive read.
`--archive-timeout` bounds the wait for Foundry archive visibility (default 420
seconds). The SDK startup deadline remains its normal default.

The installed browser test also has a real Foundry mode:

```bash
BACKPROP_TEST_PACKAGE_PATH=/path/to/installed-wheel \
BACKPROP_TEST_FOUNDRY_CONFIG=/path/to/foundry-config.json \
node tests/browser_local_backend_qa.mjs
```

The test keeps the real bearer token in a Node HTTP proxy. Browser storage holds
only a fixture session token. The proxy forwards only to the exact configured
Foundry origin, refuses redirects, and scopes reads to the test project and its
new stream range. Metrics, history, files, downloads, and stop writes still use
real Foundry resources. This does not validate interactive OAuth consent or a
production website deployment. Perform a normal sign-in/consent/refresh/sign-out
check on the release website before release; automated protocol fixtures cover
those mechanics without a personal browser session.

## Foundry backlog regression

Real acceptance can expose problems that a small fixture cannot. The existing
Foundry stream had roughly 39,000 live events during this work. Reading 100 rows
per round trip exceeded the normal startup deadline. Foundry now uses larger
adaptive pages, at most four parallel read requests, and negotiated gzip responses.
Tests prove that short pages and sparse offsets cannot advance past unseen data.
Corrupt compressed bodies retain the existing four-attempt retry and page reduction
behavior. The local adapter retains its independent loopback and durability policy.
