# Foundry Website Hosting setup

Backprop is a static Developer Console website backed by Foundry streams and a file
dataset; it needs no API server or Ontology. Run commands from the checkout root with
Python 3.10+. Replace all `<PLACEHOLDERS>` before use.

Use your enrollment's Foundry host and resources throughout this guide. You can
configure the installation in Developer Console and upload the website ZIP manually, or
use the optional operator scripts.

## 1. Prerequisites

**Manual / administrator:** arrange these permissions and resources:

- A Foundry account, Developer Console access, and a destination project.
  Under current default roles, application creation needs **Editor/Owner** on
  the parent plus the organization's **Create third-party applications**
  workflow. Linked OAuth management typically needs **Third-party application
  administrator**. Website deployment needs **Deploy application website**;
  viewers need **View application website**. Legacy/custom roles differ.
  See [permissions].
- Website Hosting availability and an **Information Security Officer** to approve
  a domain. Unsupported/custom-domain enrollments may need Palantir Support and
  DNS/TLS/ingress configuration. See [hosting].
- Someone with **Manage OAuth 2.0 clients** to enable the application for the
  intended organization. OAuth cannot exceed the user's own permissions.
  See [enablement] and [restrictions].
- The storage in section 4. Browser users need stream/dataset reads and SQL
  execution/results access. Training writers additionally need stream publishing
  and media-file writes; their credentials are separate from browser OAuth.

**Secrets:** the browser uses a **public OAuth client** with PKCE, not a client secret.
Neither state file nor the website ZIP may contain bearer tokens, client secrets,
passwords, or refresh tokens. Client IDs and resource RIDs are public configuration, not
credentials.

Operator scripts use [`FoundryApi`](../deploy/foundry_api.py) and obtain a token in this
order: `BACKPROP_TOKEN`, `FOUNDRY_TOKEN`, then the optional Google Cloud Secret Manager
source in the deployment config. Supply environment tokens through your usual secret
setup. Google Cloud is required only when using this option; in that case, `gcloud` must
be available and authenticated with permission to access the configured secret. ZIP
builds and manual uploads need no operator token or Google Cloud credentials.

## 2. Local deployment configuration

Copy the tracked template, then edit its `host`:

```bash
cp deploy/config.example.json deploy/config.local.json
```

The minimum deployment config is:

```json
{
  "host": "https://<FOUNDRY_HOST>",
  "deployment_state": "state.json",
  "resource_state": "../backend/state.json"
}
```

`host` is the Foundry HTTPS origin, with no path, credentials, query or fragment. State
paths are resolved relative to the config file, not the current working directory. With
this layout, they select `deploy/state.json` and `backend/state.json`. Keep those files
local: the default config and both state files are ignored by Git. The tracked
`deploy/config.example.json`, `deploy/state.example.json` and
`backend/state.example.json` are public templates. For additional files inside the
checkout, use a `*.local.json` name so the same ignore rule applies, or store them
outside the repository. Never add tokens to any config or state file.

Every operator CLI accepts `--config PATH`. You can instead set `BACKPROP_DEPLOY_CONFIG`
to the config path; without either selector, commands use `deploy/config.local.json`.
The Python SDK has a separate selector: `BACKPROP_CONFIG` points to the resource state
file, not the deployment config.

If you use Google Cloud Secret Manager, add this optional object to the deployment
config:

```json
{
  "secret_manager": {
    "project": "<GOOGLE_CLOUD_PROJECT>",
    "secret": "<FOUNDRY_TOKEN_SECRET>",
    "version": "latest"
  }
}
```

`version` defaults to `latest`. This specifies where to retrieve the token; it does not
store the token. Environment tokens take precedence.

## 3. Application, hosting, and browser access

**Manual / Developer Console:**

1. Select **+ New application**. Choose the destination project and
   **Client-facing application** only; decline Ontology SDK generation.
   Do not also choose Backend service: hosting requires a public client.
   Record the application RID and linked OAuth client ID.
   See [create application], [permissions], and [restrictions].
2. Keep **OAuth & restrictions → Application restrictions → Restricted**.
   In **Platform SDK**, add the Backprop project and required read operations.
   API-level security requires permitting each endpoint the browser calls:
   SQL execute/status/results/cancel, stream end-offsets/records, and dataset
   file content. [`web/api.js`](../web/api.js) is the endpoint inventory.
   See [restrictions].
3. Confirm authorization-code/PKCE and refresh-token grants. Backprop requests
   `api:use-streams-read`, `api:use-datasets-read`,
   `api:use-sql-queries-execute`, `api:use-sql-queries-read`, and `offline_access`.
   These names are from [`web/auth.js`](../web/auth.js), not interchangeable
   configuration fields. Your enrollment must accept the requested scopes. The
   [stream-read API] documents `api:streams-read`; consult your enrollment's
   administrator if consent rejects the stream scope. Adding an allowed
   operation alone does not change the scopes requested by Backprop. See [OAuth].
4. Enable the linked client for the organization using its management controls
   or **Control Panel → Third-party applications → Actions → Enablement settings**.
   Check project, operation, and marking restrictions; retain user consent.
   See [enablement].
5. In **Website hosting**, select/request the application domain and obtain
   approval. Copy Foundry's exact HTTPS root URL. Register
   `https://<APP_HOST>/` as the OAuth redirect, including `/`.
   **Do not copy the generic `/auth/callback` example:** Backprop exchanges the
   code at the root using `config.app_url`. See [hosting] and [OAuth].
6. In **Control Panel → CORS**, have an Information Security Officer or Enrollment
   Administrator allow the website origin, `https://<APP_HOST>` (no path), on the
   Foundry host receiving token/API requests if not already permitted. If the CORS
   feature is unavailable, contact Palantir. Scripts do not configure it. See [CORS].
7. In **Website hosting → Advanced → Content Security Policy**, permit the
   requests needed by the generated ZIP policy: the Foundry origin in
   `connect-src`, self-hosted scripts, inline styles, and blob-backed
   media/workers. Match any map-provider allowances. Hosting CSP and the page's
   CSP both apply; editing the ZIP does not change server CORS. See [hosting] and
   [`deploy.py`](../deploy/deploy.py).

## 4. Storage and non-secret state

**Manual / administrator:** obtain or create resources matching
[`backend/provision.py`](../backend/provision.py):

- Event stream dataset: `master`, one partition, `LOW_LATENCY`, uncompressed.
  All fields are non-nullable: `schema_version` (integer); `event_id`, `run_id`,
  `project`, `entity`, `event_type`, `timestamp`, `payload` (strings).
  Key field: `run_id`; payload contains a JSON string. Archive history must be
  queryable through SQL. See [create stream].
- Ordinary media-file dataset with `master` available for reads/uploads.
- Stream view RID from the stream API's `viewRid`, not the media dataset.

For manual setup, copy the state templates and fill every required placeholder with your
resources:

```bash
cp deploy/state.example.json deploy/state.json
cp backend/state.example.json backend/state.json
```

These are public connection settings, but belong to your local installation. Do not pass
placeholder values to operator scripts as existing resource IDs. For optional fresh
provisioning in section 7, let the scripts create state instead of copying populated
templates.

`deploy/state.json`:

```json
{
  "host": "https://<FOUNDRY_HOST>",
  "project_rid": "<PROJECT_RID>",
  "organization_rid": "<ORGANIZATION_RID>",
  "application_rid": "<APPLICATION_RID>",
  "client_id": "<PUBLIC_OAUTH_CLIENT_ID>",
  "app_url": "https://<APP_HOST>/"
}
```

`backend/state.json`:

```json
{
  "host": "https://<FOUNDRY_HOST>",
  "project_rid": "<PROJECT_RID>",
  "branch": "master",
  "stream_dataset_rid": "<EVENT_STREAM_DATASET_RID>",
  "media_dataset_rid": "<MEDIA_DATASET_RID>",
  "stream_view_rid": "<STREAM_VIEW_RID>",
  "app_url": "https://<APP_HOST>/"
}
```

Keep both state files' `host` identical to the deployment config's `host`. The website
builder reads the resource state's host for its generated browser config and CSP.
Dataset identifiers must be Foundry dataset RIDs, not names/URLs. `application_rid`
selects the website; `client_id` selects OAuth. `organization_rid` is needed by the
optional run-control configuration command when bootstrap settings are not supplied.
`stream_view_rid` is optional in packaging but retain it when provisioned.
`backend.app_url` supplies SDK run links; `deploy.app_url` supplies the callback.
Project IDs support provisioning and are not published in browser config.

Optional `deploy.map_resource_origins` is a public list of HTTPS origins without paths,
credentials, or wildcards. Omission uses [`map_policy.py`](../deploy/map_policy.py)
defaults; `[]` disables those external providers, not the pinned icon source. Do not add
secrets or put this setting in backend state.

## 5. Build and publish

**Local build for your configured Foundry host:**

```bash
python deploy/deploy.py --config deploy/config.local.json --version '<NEW_VERSION>' --build-only backprop-site.zip
```

Use `X.Y.Z` with an optional prerelease suffix. This standard-library-only build makes
no API calls and loads no credentials. It packages allowlisted assets, root
`index.html`, a CSP, and generated `config.json`, not backend/SDK/operator files. Do not
ZIP the checkout or plain `web/`.

The same build command works for another enrollment when its config and state files
contain that enrollment's host and resource IDs. Inspect the public output:

```bash
python -m zipfile -l backprop-site.zip
python - <<'PY'
from pathlib import Path
import zipfile
with zipfile.ZipFile('backprop-site.zip') as archive:
    print(archive.read('config.json').decode())
    print('ZIP bytes:', Path('backprop-site.zip').stat().st_size)
    print('Uncompressed bytes:', sum(item.file_size for item in archive.infolist()))
PY
```

**Manual / Website hosting:** upload the ZIP as a new, unused release version, wait for
scanning/validation, then deploy it. Current [hosting] limits are **1,000 files and 20
MB total upload size**. The page does not specify compression accounting; check both
sizes. The builder enforces the 1,000-file limit and 20,000,000-byte limits for
individual files, total uncompressed content and the final ZIP, including generated
configuration and notices.

**Automated publish with your configured operator credentials:**

```bash
python deploy/deploy.py --config deploy/config.local.json --version '<NEW_VERSION>'
```

It uploads via the [public upload API], retries only `ScanningInProgress` for roughly
180 seconds, deploys, confirms `deployedVersion`, and saves `deployed_version`,
`archive_sha256`, and `subdomains`. It does not test browser OAuth/data access.
Upload/deploy require the application deployment permission; OAuth operator tokens also
need `third-party-application:deploy-application-website`. See the [public deploy API].

## 6. Verify and troubleshoot

**Manual, read-only:** confirm the release/domain in Console; open the root and
`/config.json` and check host/client/dataset/branch/callback consistency. Sign in as an
intended non-admin user. In browser Network/Console, check token exchange, SQL history,
stream reads, and media reads. An empty workspace is expected before logging data; use
an existing run to check charts/media after refresh.

| Failure | Check |
| --- | --- |
| Hosting unavailable | Client-facing only/public client; enrollment support and domain approval. See [hosting]. |
| Redirect rejected | Exact HTTPS root callback with `/`, matching `deploy.app_url`; client ID, not application RID. |
| Consent/invalid scope | Organization enablement, operations/markings, refresh grant, and the stream-scope discrepancy above. |
| CORS/CSP block | Allowed website origin on the receiving Foundry host; hosting CSP plus generated `connect-src`. |
| 401 / expired session | Sign in again; refresh-token grant and `offline_access`. Never embed an operator token. |
| 403 / missing history | User data access, project and endpoint restrictions, correct stream/branch/view, SQL archive availability. |
| Wrong `config.json` host | Check the selected `--config` or `BACKPROP_DEPLOY_CONFIG`, then the host in its resource state file. |
| ZIP rejected / scan timeout | Root `index.html`, complete assets, total size/count limits, unused version; wait for scanning in Console. |

## 7. Optional operator automation

These commands create or update Foundry resources. Review your target config,
credentials and resource permissions first. Use a separate config and fresh state paths
for a new installation. Do not reuse another installation's state files. For fresh
provisioning, leave the selected state files absent or initialize them as `{}`. Existing
resource IDs are reused, and placeholder strings are not valid resource IDs.

Bootstrap needs explicit destination and owner IDs in the config:

```json
{
  "bootstrap": {
    "space_rid": "<DESTINATION_SPACE_RID>",
    "organization_rid": "<ORGANIZATION_RID>",
    "owner_user_id": "<OWNER_USER_ID>"
  }
}
```

This optional object is needed only for bootstrap. Manual setup, ZIP builds, deployment
and storage provisioning do not need it.

```bash
python deploy/bootstrap.py --config deploy/config.local.json
python backend/provision.py --config deploy/config.local.json --project-rid '<PROJECT_RID>'
# After both state files exist and the domain is registered:
python deploy/configure_domain.py --config deploy/config.local.json --app-url 'https://<APP_HOST>/'
```

- [`bootstrap.py`](../deploy/bootstrap.py) uses the supplied space, organization
  and owner IDs to create a missing project/application/client and organization
  installation. It resolves the organization's marking UUID, starts with empty
  redirects and read-only operations, and saves IDs to the selected deployment
  state file. Existing installations are not reconciled.
- [`provision.py`](../backend/provision.py) creates missing datasets, records the
  stream view RID, and saves the configured Foundry host and `master` branch to
  the selected resource state file. There is no schema migration.
- [`configure_domain.py`](../deploy/configure_domain.py) verifies website
  `subdomains`, updates the public client's grants/redirect, and writes both
  app URLs plus `domain_approved`/`subdomains`. It accepts HTTPS root URLs without
  ports/credentials/query/fragment. Its approval flag is not an independent
  approval-status check. Bootstrap also records `project_path`; none of this
  metadata is required for manual ZIP upload.
- Application/client and Multipass setup calls are internal APIs, unlike the
  public website APIs. These setup endpoints can vary by enrollment; use the
  manual Console steps if they are unavailable. Stream creation and runtime
  reads/publishing use preview APIs, which must be available in your enrollment.
  Scripts do not reconcile current API-level operation restrictions or resolve
  scope-spelling differences.
- Configure Google Cloud Secret Manager and its IAM permissions separately if
  you use it. User access, domain approval, CORS, hosting CSP and custom-domain
  DNS/TLS/ingress are also configured manually.
- Run-stop controls need stream-write permission in the application **and**
  installation, user write access, and explicit browser consent.
  [`configure_controls.py`](../deploy/configure_controls.py) accepts the same
  `--config` selector but cannot upgrade the explicit read-only installation
  created by bootstrap.
- [`backend/verify.py`](../backend/verify.py) is **not read-only**: it publishes
  probe events/uploads a file. It accepts `--config` to select the installation
  and operator credentials, and requires SDK dependencies plus permission to
  write probe data.

[permissions]: https://www.palantir.com/docs/foundry/developer-console/permissions/
[hosting]: https://www.palantir.com/docs/foundry/developer-console/deploy-custom-application-on-foundry/
[restrictions]: https://www.palantir.com/docs/foundry/developer-console/application-restrictions/
[create application]: https://www.palantir.com/docs/foundry/developer-console/create-application/
[OAuth]: https://www.palantir.com/docs/foundry/platform-security-third-party/writing-oauth2-clients/
[enablement]: https://www.palantir.com/docs/foundry/platform-security-third-party/enabling-3pa-access/
[CORS]: https://www.palantir.com/docs/foundry/administration/configure-cors/
[create stream]: https://www.palantir.com/docs/foundry/api/v2/streams-v2-resources/datasets/create-streaming-dataset/
[stream-read API]: https://www.palantir.com/docs/foundry/api/v2/streams-v2-resources/streams/get-records-from-stream/
[public upload API]: https://www.palantir.com/docs/foundry/api/v2/third-party-applications-v2-resources/versions/upload-version/
[public deploy API]: https://www.palantir.com/docs/foundry/api/v2/third-party-applications-v2-resources/websites/deploy-website/
