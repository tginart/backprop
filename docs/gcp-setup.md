# Google Cloud backend

GCP is the third backend, alongside Foundry and local SQLite. The SDK APIs,
event schema, replay rules, dashboard, artifacts, offline sync, resume, and
cooperative stop behavior stay the same. Install the optional dependencies with
`python -m pip install '.[gcp]'` from a source checkout.

## 1. Deployment prerequisites

One installation uses a Cloud Run service, a named Firestore Native database, a
private GCS bucket, a dedicated runtime service account, and an Artifact Registry
repository. All resource names,
the project, region, object prefix, OAuth client, and authorized emails come from
operator configuration. The runtime account gets `roles/storage.objectAdmin`
on its bucket and `roles/datastore.user` with an IAM condition for only its
named database. Cloud Build uses the project's configured build identity; that
identity needs access to the staging bucket and image repository.

Use an existing billed project with authenticated gcloud credentials. Required
APIs and IAM permissions must already be available. Print the prerequisites
without credentials, a resource ledger, or any cloud operation:

```bash
python deploy/gcp_deploy.py --requirements
cp deploy/gcp.example.json deploy/gcp.local.json
```

The source constants in `deploy/gcp_deploy.py` list the exact API names and
permissions by identity. The deployer needs permission to create, inspect, and
delete installation resources, submit builds, attach the runtime account, and
set the installation bucket's IAM policy. The configured Cloud Build identity
needs object access to the staging/log bucket and image read/write access to the
repository. Google's Cloud Build and Cloud Run service agents must retain their
standard service-agent roles. These are prerequisites; the script does not enable
APIs or grant unconditioned project-wide access. Google implements database-scoped
Firestore access through a conditional project IAM binding. The deployer therefore
needs project IAM read/write permission. The script grants the new runtime account
access to only the new bucket and the exact new database; cleanup removes that
same conditional binding. Concurrent IAM changes and new account propagation have
bounded retries.

Edit `deploy/gcp.local.json`. Set an explicit project, region, and new installation
name. Set `allowed_emails` to the Google users and service accounts that can read
and write this installation's data. The service enforces this list independently
of Google's consent settings. Keep operator configuration and deployment state
local; do not commit them.

**Manual, once:** register the Google OAuth Web client described below. This is
the equivalent of the Foundry application's OAuth registration. The operator
needs **OAuth Config Editor** (`roles/oauthconfig.editor`) or equivalent
permissions for this step. Google requires
[manual client creation and configuration](https://developers.google.com/identity/protocols/oauth2/resources/best-practices#manual_creation_and_configuration_of_oauth_clients).

**Scripted, per installation:** create the bucket, named Firestore database,
required indexes, runtime service account, image repository, and Cloud Run service;
build the checkout; wire the client ID and
allowlist into the service; save its URL and SDK connection file. The script
stops if a resource name already exists. It does not adopt another application's
resources or choose a project from gcloud's defaults.
`firestore_database` defaults to the new installation name and
`firestore_location` defaults to `region`. Both can be configured explicitly.
The script never adopts the project's `(default)` database or an existing named
database. Runtime settings also require an explicit project and database.

## 2. One-time Google OAuth setup

1. Open [Google Auth Platform](https://console.cloud.google.com/auth/overview).
   Select the same project as `project` in your deployment configuration.
2. If Google shows **Get started**, complete the application registration. Use
   an application name such as **Backprop**, your support email, and your
   developer contact email. Under **Audience**, choose **External** for personal
   Google accounts such as Gmail. Choose **Internal** only when all users belong
   to the project's Google Workspace organization.
3. For a test installation, keep the audience in **Testing** and add your test
   accounts under **Audience → Test users** if Google requests them. Backprop
   requests only `openid`, `email`, and `profile`; it does not request browser
   access to Cloud Storage or the cloud-platform scope. In **Data Access**, use
   those basic identity scopes when Google asks for scope configuration.
4. Open **Clients → Create client**. Select **Web application**, and give the
   client a distinct name such as **Backprop dashboard**. For a first deployment,
   leave **Authorized JavaScript origins** empty until Cloud Run supplies the
   URL in step 3 below. Leave **Authorized redirect URIs** empty: this adapter
   uses Google's popup token flow and has no OAuth callback route.
5. Create the client. Copy only its **Client ID**, which ends in
   `.apps.googleusercontent.com`, into `client_id` in `deploy/gcp.local.json`.
   This browser flow does not use a client secret. Do not copy the downloaded
   credentials JSON, client secret, or access token into this file.

Branding and audience settings apply to the Google project. If the project
already has those settings, review them with the project owner rather than
changing them for this installation. Create a separate Backprop Web client;
do not edit an existing production application's client.

You can retain the Web client for later deployments. Register each new service
origin on that client. An update that retains the same service URL needs no new
OAuth registration. See Google's [client setup](https://developers.google.com/identity/oauth2/web/guides/get-google-api-clientid)
and [Auth Platform configuration](https://support.google.com/cloud/answer/15544987).

## 3. Deploy the installation

```bash
python deploy/gcp_deploy.py --config deploy/gcp.local.json \
    --state .backprop/gcp/state.local.json
```

The command builds this checkout, creates dedicated resources, deploys the service,
sets its actual public origin, and downloads a connection file beside the resource
ledger. Cloud Run assigns the URL to this new service; it is not another
application's URL. Save the returned `origin` and the
`browser_authentication.authorized_javascript_origin` value for the next step.
The script checks that the deployed client ID matches your input. Its `deployed`
status confirms infrastructure deployment; the browser authentication status
explicitly requires origin registration and a real sign-in check.

A missing or malformed `client_id` stops deployment before any cloud resource or
ledger is created. If you intentionally need no browser sign-in, remove
`client_id` and set `"sdk_only": true`; the output then identifies SDK-only mode.
This mode does not provide a working dashboard sign-in.

Failures after resource creation preserve the ledger for cleanup. New Google
service accounts have bounded propagation retries. The command does not enable
APIs or change project IAM automatically.

Cloud Run uses zero minimum instances, four maximum instances, concurrency four,
a 300-second timeout, and 4 GiB memory. `max_instances` is configurable. The bucket
has uniform access and public access prevention. Soft-delete retention is disabled
so temporary resource cleanup removes stored bytes. Staged file parts have a
one-day deletion lifecycle rule.

## 4. Register the origin and verify sign-in

1. Open the `browser_authentication.clients_url` returned by the deployment
   command. Open the Web client whose ID matches `client_id` in your config.
2. Under **Authorized JavaScript origins**, add the exact
   `browser_authentication.authorized_javascript_origin` value. Use the HTTPS
   scheme and hostname only: no trailing slash, path, query, or wildcard. Use
   this canonical origin even if Cloud Run displays an alternate service URL.
   Save the client and allow Google's change to propagate. No second deployment
   is needed after this origin registration.
3. Open the returned `origin` in a regular browser. Allow the Google popup,
   select **Sign in with Google**, and choose an account in `allowed_emails`.
   After consent, confirm that the dashboard loads experiment data, then reload
   it and confirm that the session still works. Finally, sign out and confirm
   that data access requires sign-in again. Empty installations need SDK data
   before the dashboard can show experiments.

The existing browser acceptance suite tests dashboard operations with a real
Google token. It does not replace this interactive Google account-chooser check.
The deployment command cannot verify a person's consent automatically. Google's
[token-model guide](https://developers.google.com/identity/oauth2/web/guides/use-token-model)
describes this popup flow.

| Symptom | Check |
| --- | --- |
| `origin_mismatch` | Add the exact returned origin to the same Web client's Authorized JavaScript origins. |
| `invalid_client` | Use a Web application client and copy its full Client ID into `client_id`. |
| Google refuses consent | Review Audience, any required test users, and Workspace administrator policy. |
| Backprop refuses the Google account | Add its verified email to the deployment's `allowed_emails`. Google consent alone does not grant access. |
| Popup blocked or cancelled | Allow popups for the service origin and retry sign-in. |

## 5. Connect the SDK

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

Firestore holds immutable event documents, partition cursors, file references,
query descriptors, and the stable installation identity. GCS holds artifact bytes
and temporary CSV history results. No SQLite database, shared filesystem, process
lock, or always-running database instance participates in GCP persistence.

Each publication transaction reads only the incoming event fingerprints and one
partition cursor. It atomically creates the new event documents and advances
that cursor. A batch of N new events requests N+1 document reads and N+1 writes,
before any contention retries. This work does not depend on the existing history
size. Identical retries consume no offsets; conflicting IDs reject the whole batch.
SHA-256 document IDs distribute event identities. Each batch selects one of 16
partitions from its first new event ID, so there is no global counter to serialize
all writers. Contention retries remain transient service failures if exhausted.

The existing dashboard polls incremental records with a separate cursor for each
partition. On initial load, it merges history and the incremental tail through
the existing shared browser code. Live mode refreshes every 30 seconds while the
page is visible. This is the existing HTTP publish/read protocol.

History queries use exact hashed scope keys, native indexes, bounded cursor pages,
and a merge across partitions. Event payloads are unindexed canonical JSON strings.
A bounded sort prefix keeps indexed values below Firestore's field limit; complete
prefix ties retain exact timestamp/event-ID order. Long IDs and deeply nested
payloads therefore retain the shared contract without oversized index entries.

Every archive query captures one Firestore read time and its full partition
boundary vector. The service materializes an immutable CSV at that read time and
streams it from GCS with bounded server memory. A later write cannot enter an
earlier snapshot, including a backdated event. Results survive service restarts
for one hour. Cancellation removes query metadata and cached bytes; Firestore TTL
and the GCS one-day lifecycle remove expired cache resources asynchronously. The
API enforces the exact one-hour expiry independently of TTL deletion time.

Artifact bytes become durable in GCS before a Firestore reference commits.
Competing APPEND claims retain the first winner. Downloads verify size and SHA-256.

The existing 900 KB event, 500-record batch, 512 MiB file, and one-million-event
browser limits apply. Files above 8 MiB use bounded staging parts to respect
Cloud Run's HTTP/1 request limit. Large responses use chunked transfer, including
range responses. Staged parts remain retryable until lifecycle deletion.

Firestore bills document operations, index reads where applicable, and stored
data. Large history reads still incur proportional read costs; the browser retains
its one-million-event limit. Pathological long-ID prefix ties require a complete
tie-group read. There is no automatic event retention or general orphan-artifact
collector. Load-test the expected batch sizes, writer concurrency, and query scope.
See Google's [transactions](https://firebase.google.com/docs/firestore/manage-data/transactions),
[index and hotspot guidance](https://firebase.google.com/docs/firestore/best-practices),
[billing](https://firebase.google.com/docs/firestore/pricing), and
[browser token model](https://developers.google.com/identity/oauth2/web/guides/use-token-model).

## Verify and clean up

Run the same SDK acceptance scenario as the other backends:

```bash
python tests/backend_acceptance.py --backend gcp \
    --gcp-config /path/to/connection.json --report /tmp/gcp-acceptance.json
```

The scenario checks metrics, history, tables, Plotly, artifact integrity,
committed-response loss, replay, atomic claims, stop, resume, offline sync, and an
independent archive read. GCP-specific tests use Google's Firestore emulator and
cover concurrent instances, atomic conflicts, isolation, snapshot races, Unicode
cursor pages, large/deep payloads, query expiry, corrupt artifacts, chunked files,
ADC refresh, and Google browser authentication. Install Java 21, then run:

```bash
python tools/firestore_emulator.py -- python -m unittest discover \
    -s tests -p 'test_gcp_backend.py'
```

The harness downloads a pinned, SHA-256-verified Google emulator into the ignored
local cache. It starts a loopback service, supplies only synthetic project names,
and stops the emulator after the command exits. The same harness runs in CI.

For the complete source/package/browser matrix:

```bash
export BACKPROP_GCP_ACCESS_TOKEN="$(gcloud auth print-access-token)"
python tools/firestore_emulator.py -- python tools/verify_backends.py \
    --gcp-config /path/to/connection.json
```

Measure native ingestion at increasing history sizes and with 1, 4, and 8 writers
using only a disposable installation's resources:

```bash
python tools/benchmark_gcp.py --project YOUR_PROJECT --database YOUR_TEST_DATABASE \
    --bucket YOUR_TEST_BUCKET --report .backprop/gcp/benchmark.local.json
```

The report records latency, requested document reads/writes, request bytes, and
GCS calls. Its operation counts are client observations, not a cloud billing
audit. The test creates a unique prefix and appends data. Delete the temporary
database through its ownership ledger after verification.

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

Cleanup removes the service, named Firestore database, exact conditional IAM
binding, image repository, runtime account, and bucket, including experiment data
and build staging objects. It leaves enabled Google APIs enabled. Failed removals stay in the
ledger for retry. Successful cleanup marks the ledger `deleted`. Preserve data
you need before cleanup. Cleanup leaves your reusable OAuth client and project
consent configuration intact. Remove a deleted service's origin from the client;
delete a dedicated OAuth client in Google Auth Platform when it is no longer needed.

## Updates and backup

Build a new image and update the existing Cloud Run service. Preserve its Firestore
database, bucket, prefix, public origin, OAuth client, and allowlist. Keep the prior image digest for
runtime rollback. A runtime rollback must not roll back stored experiment data.

For a consistent backup, stop writers and wait for flushes. Export the selected
Firestore database through Google's managed export, and copy the complete GCS
installation prefix into separate private backup storage. Restore all event,
cursor, file-reference, and installation metadata together with the artifact bytes.
Preserve the bucket name and prefix used to identify the installation namespace.
The Firestore installation descriptor preserves `gcp_id`. Back up service settings,
IAM conditions, OAuth configuration, and SDK journals separately. A restore must
not discard events that clients already durably acknowledged.

## Validation record

The Firestore implementation requires fresh native, shared-contract, package,
browser, and real-deployment evidence. The earlier GCS/SQLite snapshot deployment
does not validate this implementation. Temporary installations use dedicated
ownership ledgers. Verification reports and deployment state stay outside public
source; record observed results only after the corresponding checks pass.
