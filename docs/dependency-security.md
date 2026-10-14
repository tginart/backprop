# Dependency security

Backprop runs in your training environment and Foundry enrollment. Keep its Python
dependencies and browser assets current, and review the optional frameworks and external
providers you enable.

## Browser renderers

The browser bundles Vega 6.2.0, Vega-Lite 6.4.1 and Expression Interpreter 2.2.1. These
include fixes for [event-filter
XSS](https://github.com/vega/vega/security/advisories/GHSA-rcw3-wmx7-cphr), [RegExp
replacement XSS](https://github.com/vega/vega/security/advisories/GHSA-963h-3v39-3pqf),
[global gadget
XSS](https://github.com/vega/vega/security/advisories/GHSA-7f2v-3qq3-vvjf) and [setdata
XSS](https://github.com/vega/vega/security/advisories/GHSA-m9rg-mr6g-75gm). Custom
charts use the expression interpreter, reject external URLs and dangerous object keys,
and run under the website content security policy. Backprop does not expose a Vega View
instance or `VEGA_DEBUG` to chart expressions.

Plotly strict 4.1.1 is the default renderer. Strict 3.6.0 handles legacy Mapbox traces
that Plotly 4 removed. The legacy bundle includes a map dependency affected by [MapLibre
attribution XSS](https://github.com/advisories/GHSA-jrc7-96c5-q579). Backprop escapes
style, TileJSON and map-layer attribution text before passing it to either renderer, and
its CSP blocks inline event handlers. Keep those protections enabled. The legacy
dependency remains affected; these mitigations do not replace an upstream fix.

External map providers have their own authentication, CORS and resource policies.
Configure permitted origins in your deployment, and allow only providers you trust.
Foundry credentials are not sent to map providers.

Renderer sources and hashes are recorded in
[`vega-provenance.json`](../web/vega-provenance.json); dependency versions are pinned in
[`renderer-package-lock.json`](../web/renderer-package-lock.json). Deploy the browser
assets, CSP and notices together.

## Python dependencies

Optional extras declare their dependencies in [`pyproject.toml`](../pyproject.toml). The
maintained framework groups require patched dependency floors, including Torch >=2.14.1,
Lightning >=2.6.6 with fsspec >=2026.7.0, LightGBM >=4.7, scikit-learn >=1.5,<2, Pillow
>=12.3, and TensorFlow 2.18.x with Keras >=3.15.1 and protobuf >=5.29.6,<6. Resolve
these constraints in an isolated training environment and check it when updating
frameworks or combining extras.

**Legacy fastai 1:** its Torch 1.13.1/torchvision 0.14.1 stack and historical HTTP
dependencies have known upstream advisories. The `fastai` extra is retained for legacy
compatibility and is not recommended for new installations. Use an isolated environment
if you need it; compatibility does not establish security.

The `video` and `gym` extras use MoviePy 1.0.3, NumPy >=1.26,<2, Gymnasium 1.0.x and
Pillow >=12.3. MoviePy 2.2.1 requires Pillow <12 and cannot satisfy the package's Pillow
floor. Replacing the encoder requires a compatible dependency set as well as changes to
its integration.

## Model files

Backprop's model helpers transfer checkpoint bytes without unpickling or loading models.
Your framework's model loader may execute code from a checkpoint. Load only trusted
checkpoints with a maintained framework.
