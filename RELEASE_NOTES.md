# Release notes

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
