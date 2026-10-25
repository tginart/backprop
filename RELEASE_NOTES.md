# Release notes

## 0.1.2

Run Backprop locally with SQLite and the packaged dashboard, or keep your existing
Foundry connection ([#5](https://github.com/tginart/backprop/pull/5)).

- Add `backprop serve DIRECTORY` for a loopback dashboard and storage API.
- Select local or Foundry through one connection file. Local mode needs no token or OAuth.
- Import existing SDK journals with a separate local ingestion cursor. Preserve source
  records and Foundry acknowledgements, deduplicate retries, and retain stream offsets.
- Preserve imported linked-media choices through source edits, deletion, server restart,
  lost responses, and concurrent imports. Keep artifact integrity and atomic claims.
- Support metrics, history, resume, tables, Plotly, artifacts, preferences, and cooperative
  stop controls through the existing SDK and browser interfaces.
- Package dashboard assets in wheels and source distributions. Document local startup,
  SSH forwarding, journal import, backup/restore, and verification.
- Retain Foundry support and improve bounded archive/live reads, gzip retries, and login
  deadlines. Completed local artifact rounds no longer suppress later identical rounds.

Existing training code, public SDK APIs, and event formats remain compatible. Local mode
requires an explicit local connection and a filesystem with reliable locks and fsync.
Use SSH forwarding for remote access; the local server has no multi-user authentication.

## 0.1.1

Browser workspace improvements from three merged pull requests:

- Preserve scroll, filters, chart controls and expanded views across Foundry refreshes
  ([#1](https://github.com/tginart/backprop/pull/1)).
- Add live scalar-chart smoothing, raw-data overlays and magnetic point inspection
  ([#2](https://github.com/tginart/backprop/pull/2)).
- Make expanded charts fill the available viewport and retain focus across redraws
  ([#3](https://github.com/tginart/backprop/pull/3)).

The Python API and event format remain unchanged from 0.1.0.

## 0.1.0

Initial public release of Backprop: experiment tracking on Palantir Foundry with a
W&B-compatible Python API and a browser workspace.

- Run lifecycle, metrics, configuration, summaries and offline logging.
- Tables, media, artifacts, shared writers and selected framework integrations.
- Run comparison, charts, files and artifact inspection in the browser.
- Foundry resource setup and static website ZIP deployment.
- Bounded browser/OAuth requests, SQL polling and cancellation deadlines.
- Apache-2.0 licensing, integration provenance and third-party notices.

### Compatibility and limits

Backprop targets the experiment-tracking APIs of W&B 0.30.0. See [API
support](docs/python-api-support.md) and [compatibility](docs/compatibility.md) for
supported APIs and Foundry differences.

Use the documented website ZIP installation path; Marketplace/AIP distribution is not
supported. Configure Foundry permissions and external-provider IAM for your
installation. Multi-device GPU capture, legacy Windows console hosts and some Plotly
configurations are experimental.

Legacy fastai 1 has unresolved dependency advisories. See [dependency
security](docs/dependency-security.md) for details and renderer limits.

Foundry website versions are deployment identifiers and can differ from the SDK version.
Use a new, unused website version when deploying, and follow the [updates and rollback
guide](docs/updates.md).
