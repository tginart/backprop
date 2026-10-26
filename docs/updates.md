# Updates and rollback

Keep the SDK package, source reference, website ZIP and SHA-256 hashes for each
installed version. Back up your deployment config and state privately; they contain the
resource bindings needed to restore the installation.

Release tags such as `v0.1.3` identify the SDK and browser source shipped together. The
Python package and `backprop.__version__` use `0.1.3`. Foundry website versions are
deployment identifiers and can differ from the SDK version. Each website upload needs a
new, unused version.

## Update the SDK

Finish active writers and preserve their offline journals. Install the target release in
your training environment. For example, to install 0.1.3:

```bash
python -m pip install --upgrade 'backprop-foundry @ git+https://github.com/tginart/backprop.git@v0.1.3'
python -m backprop --help
```

You can also install its wheel. Run a small offline experiment and check the optional
integrations your training code uses before starting online jobs.

To roll back, reinstall the previous package or wheel. Preserve event journals; do not
rewrite them to match an older SDK. Event-format changes may require a matching reader,
so check release notes before combining SDK and app versions. Repeated offline sync does
not intentionally duplicate acknowledged events.

## Update a Foundry browser app

Use the target release's source checkout and your installation's local config. Replace
`VERSION` below with a new website version. Build a ZIP for manual upload:

```bash
python deploy/deploy.py --config deploy/config.local.json --version VERSION --build-only website.zip
```

Check its resource bindings and content security policy, then upload and deploy it in
Foundry Developer Console. To upload and deploy with operator credentials:

```bash
python deploy/deploy.py --config deploy/config.local.json --version VERSION
```

See [Foundry setup](foundry-setup.md) for permissions and configuration. After updating,
sign in and check charts, files and artifacts from an existing run.

To roll back, redeploy the retained previous website version in Developer Console. If
required, upload its retained ZIP under a new website version. Keep the same resource
bindings: rolling back the app does not revert or delete datasets.

## Update a local installation

Stop active writers, flush pending journals, and stop the local server. Back up the
complete data directory before the package upgrade. Install the target package
version, then restart `backprop serve` with the same data directory. The dashboard
assets come from the installed package. Verify existing runs, artifact downloads,
and a small new experiment before resuming other writers.

To roll back, stop the server and reinstall the retained previous package. Preserve
current data and journals. If the release changes the storage format, follow its
migration instructions before restoring a backup. See the
[local backend guide](local-backend.md) for consistent backup and restore steps.

## Update a Google Cloud installation

Build the target release's container image and update the existing Cloud Run
service. Preserve its named Firestore database, GCS bucket and prefix, public
origin, OAuth client, allowlist, and runtime identity. Install the matching SDK
package on training machines and retain their journals. Do not run the new-install
provisioner against an existing installation or use cleanup as an upgrade step.

Retain the prior image digest and service settings for runtime rollback. A runtime
rollback does not roll back stored experiment data. Verify Google sign-in, existing
history and artifacts, and a small new experiment after the update. See
[Google Cloud updates and backup](gcp-setup.md#updates-and-backup) for the resource
and backup requirements.
