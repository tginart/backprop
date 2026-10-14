# Contributing

Submit a focused pull request that explains the behavior change and how to test it.
Contributions to Backprop are under Apache-2.0; preserve the attribution and licenses of
any third-party code.

## Development environment

Use Python 3.10 or newer, Node.js 24 and an isolated Python environment. From a source
checkout:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e . build setuptools wandb==0.30.0 numpy Pillow matplotlib plotly PyYAML xxhash google-re2
```

Optional framework tests require the corresponding extras. See
[`pyproject.toml`](pyproject.toml) for dependency groups and version constraints.

## Run tests

```bash
PYTHONPATH=sdk python -m unittest discover -s tests -p 'test_*.py'
node --test tests/test_*.mjs
```

Some compatibility tests need the W&B 0.30.0 source tree. Set
`BACKPROP_REFERENCE_SOURCE` to its location, or use the distribution verifier below,
which downloads the source at a fixed commit and checks its hash.

Browser tests use Playwright and Chromium. Install them in the ignored local workspace
and set the module path:

```bash
npm install --prefix .backprop/browser --no-save playwright@1.64.0
.backprop/browser/node_modules/.bin/playwright install chromium
export PLAYWRIGHT_MODULE="$PWD/.backprop/browser/node_modules/playwright/index.mjs"
```

If you use a separate Chromium installation, set `CHROMIUM_EXECUTABLE` to its
executable. Tests use local or intercepted services and need no Foundry credentials.

## Source manifest and distribution checks

`PUBLIC_MANIFEST.json` lists source files with their sizes and SHA-256 hashes. When
changing files, update their manifest entries; add or remove entries when adding or
removing source files. New documentation paths also belong in `PUBLIC_DOCUMENTATION` in
[`tools_check_release.py`](tools_check_release.py). Keep build output, credentials and
local configuration out of the manifest.

Refresh the sizes and hashes of the listed files with:

```bash
python - <<'PYTHON'
from pathlib import Path
import hashlib, json
path = Path("PUBLIC_MANIFEST.json")
manifest = json.loads(path.read_text())
for entry in manifest["files"]:
    data = Path(entry["path"]).read_bytes()
    entry.update(bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
path.write_text(json.dumps(manifest, indent=2) + "\n")
PYTHON
```

The full verifier checks a source snapshot without Git metadata. After committing your
changes and manifest, run it on an archive of that commit:

```bash
BACKPROP_CHECK_SOURCE=$(mktemp -d)
BACKPROP_CHECK_OUTPUT=$(mktemp -d)
git archive HEAD | tar -x -C "$BACKPROP_CHECK_SOURCE"
python "$BACKPROP_CHECK_SOURCE/tools_verify_release.py" \
  --root "$BACKPROP_CHECK_SOURCE" --output "$BACKPROP_CHECK_OUTPUT" --browser
```

The output contains test logs, package and website builds, and integrity checks. Use an
absolute `PLAYWRIGHT_MODULE` path so browser tests can find Playwright from the
temporary source tree.

## Fixtures and sensitive data

Use synthetic fixtures. Keep tokens, personal paths, enrollment URLs, OAuth settings,
resource IDs, experiment data and deployment state out of commits. Describe unsupported
behavior in the API docs and preserve third-party provenance.

The PEM files in `tests/` are fixed loopback TLS fixtures. They are not production
credentials. Geographic fixture sources and licenses are documented in [Geographic
browser fixtures](tests/fixtures/MAP-FIXTURES.md).
