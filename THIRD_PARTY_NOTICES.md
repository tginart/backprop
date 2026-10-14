# Third-party notices

Backprop is an independent implementation for Foundry. W&B compatibility targets
Python SDK **0.30.0** at upstream commit
`e5ec9dc9236d1a4fbb9fe7c0795bbccdff1e0e77`. W&B names identify compatible APIs
and do not imply endorsement.

The following components adapt algorithms or interfaces from that source:

| Backprop component | Adapted behavior |
| --- | --- |
| `sdk/backprop/table_types.py` | Python dtype algorithms, with local imports and handling for empty array shapes. |
| `sdk/backprop/reference_handlers/` | File, HTTP, S3, GCS, Azure and generic artifact reference handlers. |
| `sdk/backprop/integration/` | Selected framework callbacks and scikit-learn plotting helpers; W&B service calls and product-usage telemetry are replaced or removed. |
| `sdk/backprop/graph.py`, `torch_histogram.py`, `torch_graph.py` | Graph values, Keras topology, tensor histograms and observed module capture. |
| `sdk/backprop/media_base.py`, `partitioned_table.py`, `values.py` | Media binding, partition iteration and value registration. |
| `sdk/backprop/annotation_values.py`, `media.py` | Image boxes and masks, HTML, Plotly, audio and video serialization. |
| `sdk/backprop/plotly_spec.py` | Pydantic-first Plotly JSON conversion. |
| `sdk/backprop/tables.py`, `extended_tables.py`, `table_serialization.py`, `table_construction.py` | Table construction, binding, joins, JSON conversion, NumPy column files and incremental ordering. |
| `sdk/backprop/eval_table.py`, `evaluation.py` | Evaluation lifecycle, column roles and rich-value normalization, using Foundry artifacts. |
| `sdk/backprop/helper.py` | Configuration containers, framework flags and YAML loading. |
| `sdk/backprop/artifact_inputs.py` | Artifact input validation and descriptive metadata conversion. |

Source hashes are retained in component modules, reference tests and
[`upstream-sources.json`](sdk/backprop/integration/upstream-sources.json).
Derived W&B portions retain the MIT license below. Browser and Weave component
licenses are listed in the following sections and included with the distribution.

## W&B Python SDK

MIT License

Copyright (c) 2021 Weights and Biases, Inc.

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

## Vega renderers

The browser bundles Vega 6.2.0, Vega-Lite 6.4.1 and Vega Expression Interpreter
2.2.1. Their BSD-3-Clause licenses are retained as `web/vega-LICENSE.txt`,
`web/vega-lite-LICENSE.txt` and `web/vega-interpreter-LICENSE.txt`. Download URLs
and SHA-256 hashes are recorded in `web/vega-provenance.json`.

## Weave evaluation audio boundary

`sdk/backprop/evaluation_media.py` and the external-service fixture in
`tests/eval_weave_audio_fixture.py` follow the file-format checks and base64
normalization of Weave v0.52.41's
[audio adapter](https://github.com/wandb/weave/blob/v0.52.41/weave/type_handlers/Audio/audio.py).
Backprop stores the result through its own Foundry artifact path. Weave's
Apache 2.0 license is retained in `licenses/Weave-Apache-2.0.txt`.
