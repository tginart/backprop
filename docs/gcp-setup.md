# Google Cloud backend

GCP is the third backend, alongside Foundry and local SQLite. The SDK APIs,
event schema, replay rules, dashboard, artifacts, offline sync, resume, and
cooperative stop behavior stay the same. Install the optional dependencies with
`python -m pip install '.[gcp]'` from a source checkout.

## Deploy

One installation uses a Cloud Run service, a private GCS bucket, a dedicated
runtime service account, and an Artifact Registry repository. All resource names,
the project, region, object prefix, OAuth client, and authorized emails come from
operator configuration. The runtime account gets `roles/storage.objectAdmin`
on its bucket. Cloud Build uses the project's configured build identity; that
identity needs access to the staging bucket and image repository.

Authenticate gcloud and enable the required APIs in your selected project:

```bash
gcloud services enable run.googleapis.com cloudbuild.googleapis.com \
    artifactregistry.googleapis.com iam.googleapis.com storage.googleapis.com \
    --project YOUR_PROJECT
cp deploy/gcp.example.json deploy/gcp.local.json
```

Edit the copied file. Choose a new installation name, project and region. Add
the Google user or service-account emails permitted to use the installation.
Existing resource names cause deployment to stop rather than adopt resources.

For browser sign-in, create a **Web application** OAuth client in Google Auth
Platform. Configure its consent screen and test users as required by Google.
Put its public client ID in `client_id`. After deployment, add the service's exact
HTTPS origin to the client's authorized JavaScript origins. No client secret is
needed. An empty client ID supports SDK-only installations.

```bash
python deploy/gcp_deploy.py --config deploy/gcp.local.json \
    --state .backprop/gcp/state.local.json
```

The command builds this checkout, creates dedicated resources, deploys the service,
sets its actual public origin, and downloads a connection file beside the resource
ledger. Failures preserve the ledger for cleanup. New Google service accounts have
bounded propagation retries. The command does not enable APIs or change project
IAM automatically.

Cloud Run uses zero minimum instances, four maximum instances, concurrency four,
a 300-second timeout, and 4 GiB memory. `max_instances` is configurable. The bucket
has uniform access and public access prevention. Soft-delete retention is disabled
so temporary resource cleanup removes stored bytes. Staged file parts have a
one-day deletion lifecycle rule.

## Connect

```bash
gcloud auth application-default login
export BACKPROP_CONFIG=/absolute/path/to/state.local.connection.json
python training.py
```

The SDK refreshes Application Default Credentials before requests. On Google
compute, use the attached service account and allowlist that account's email.
Local user ADC needs cloud-platform and userinfo.email scopes, supplied by the
standard gcloud ADC login. The public SDK connection needs no Google project ID.

For short-lived explicit credentials, set `BACKPROP_GCP_ACCESS_TOKEN` or supply a
Google access token through the existing login/API token argument. Explicit
tokens do not refresh themselves. Implicit GCP authentication never uses Foundry
tokens, W&B environment keys, or Foundry credential caches.

The public connection selects `"backend": "gcp"`, an HTTPS origin, and its stable
`gcp_id`. Download it from the existing setup page. The adapter verifies the
installation identity before it uses a journal cursor. Journal acknowledgements
and artifact resolution caches use a separate GCP installation namespace.

The browser uses Google Identity Services. It verifies authorization with the
service before it saves an installation-scoped session in session storage.
Sign-out clears the session. Renewal uses Google's token client; if a popup is
blocked, sign in again through the existing sign-in control.

## Access and persistence

The dashboard shell and connection file are public. Every data API request
requires a Google access token. The service checks the token with Google, its
expiry, its verified email, and the explicit email allowlist. Authorized identities
can read and publish data, save workspace revisions, and request cooperative
stops. Experiment project namespaces share installation access, as with the other
backends. Never put credentials in the public connection.

Cloud Run's invoker IAM check is disabled so browsers can load the shell and send
OAuth access tokens. Application authorization protects all data endpoints.
Cross-origin requests, arbitrary SQL, unrelated Foundry endpoints, and arbitrary
GCS paths are refused. Local filesystem journal import remains a local-only tool;
the existing SDK offline sync works with GCP.

GCS holds a consistent SQLite snapshot and content-addressed artifact objects.
Each operation uses a private temporary SQLite copy to apply the existing
transaction and query rules. Writes replace only the exact GCS generation they
read. Concurrent replacement retries the complete transaction. No Cloud Run disk,
process lock, or shared database filesystem is authoritative.

Acknowledgements follow durable GCS commits. Identical retries do not consume
offsets. Conflicting IDs reject the entire batch. Artifact bytes become durable
before their references commit; competing APPEND claims retain the first winner.
Downloads verify size and SHA-256. History snapshots retain their exact stream
boundary for one hour across restarts; cancellation releases metadata and later
writes reclaim expired query metadata.

The existing 900 KB event, 500-record batch, 512 MiB file, and one-million-event
browser limits apply. Files above 8 MiB use bounded staging parts to respect
Cloud Run's HTTP/1 request limit. Large responses use chunked transfer, including
range responses. Staged parts remain retryable until lifecycle deletion.

Every database operation downloads the complete snapshot; mutations replace it.
This design suits modest installations. Large histories increase latency, memory,
and GCS costs. There is no automatic event retention or general orphan-artifact
collector. See Google's [generation preconditions](https://cloud.google.com/storage/docs/request-preconditions),
[consistency guarantees](https://cloud.google.com/storage/docs/consistency), and
[browser token model](https://developers.google.com/identity/oauth2/web/guides/use-token-model).

## Verify and clean up

Run the same SDK acceptance scenario as the other backends:

```bash
python tests/backend_acceptance.py --backend gcp \
    --gcp-config /path/to/connection.json --report /tmp/gcp-acceptance.json
```

The scenario checks metrics, history, tables, Plotly, artifact integrity,
committed-response loss, replay, atomic claims, stop, resume, offline sync, and an
independent archive read. GCP-specific tests cover concurrent instances, generation
races, isolation, query expiry, corrupt artifacts, chunked files, ADC refresh,
and Google browser authentication.

For the complete source/package/browser matrix:

```bash
export BACKPROP_GCP_ACCESS_TOKEN="$(gcloud auth print-access-token)"
python tools/verify_backends.py --gcp-config /path/to/connection.json
```

The live browser scenario uses a short-lived Google test session against the
actual deployed dashboard and APIs. It checks metrics, tables, Plotly, downloads,
preferences, workspace revisions, and stops. It does not automate personal Google
consent. Popup consent, renewal, denial, and sign-out have protocol fixtures;
check the interactive account chooser with your configured OAuth client before
opening access to users.

Delete a temporary deployment through its ownership ledger:

```bash
python deploy/gcp_deploy.py --cleanup --state .backprop/gcp/state.local.json
```

Cleanup removes the service, image repository, runtime account, and bucket,
including experiment data and build staging objects. Failed removals stay in the
ledger for retry. Successful cleanup marks the ledger `deleted`. Preserve data
you need before cleanup.

## Updates and backup

Build a new image and update the existing Cloud Run service. Preserve its bucket,
prefix, public origin, OAuth client, and allowlist. Keep the prior image digest for
runtime rollback. A runtime rollback must not roll back stored experiment data.

For a consistent backup, stop writers, wait for flushes, then copy the complete
installation prefix into a separate private bucket. Restore the snapshot and all
artifacts together. The snapshot preserves `gcp_id`. Back up service settings,
bucket permissions, OAuth configuration, and SDK journals separately.

## Validation record

Validated on 2026-10-25 from a clean worktree based on fetched `origin/main`
revision `c4ec90a3e28b61d5b491a9a4ada35a7cbaefc280`:

- Existing matrix: 1,472 Python tests, with 190 optional-runtime skips;
  338 JavaScript tests passed, with six optional-runtime skips.
- Wheel/sdist parity, installed CLI restart, and local SDK/browser acceptance passed.
- A real Cloud Run/GCS deployment passed shared SDK and dashboard acceptance.
- Real concurrent writes, competing claims, revision replacement, Google access
  checks, and a 512 MiB upload/download with SHA-256 verification passed.
- Google-specific regression coverage also passed after final review fixes.

Temporary installations use dedicated resource ledgers and the cleanup command
above. All test installations were deleted. Independent listings confirmed zero
remaining test services, buckets, repositories, and service accounts. Verification
reports and deployment state stay outside public source.
