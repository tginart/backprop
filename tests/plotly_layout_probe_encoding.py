"""Isolated pinned/local independent encoding for repaired layout fixtures.

Architecture: process import boundary -> genuine Figure -> three alias files.
Main caller: test_plotly_layout_encoding. No rendering claim follows from bytes.
"""
import hashlib
import importlib
import inspect
import json
from pathlib import Path
import sys
from plotly_layout_fixtures import encoded_figures

if sys.argv[1] == "reference":
    import wandb as w
    assert w.__version__ == "0.30.0"
else:
    import backprop as w
    from backprop.compatibility import install_wandb_aliases
    install_wandb_aliases()
result = {"aliases": {}}
if sys.argv[1] == "reference":
    result["source_sha256"] = hashlib.sha256(Path(inspect.getfile(w.Plotly)).read_bytes()).hexdigest()
modules = ["wandb", "wandb.data_types", "wandb.sdk.data_types.plotly"]
if sys.argv[1] != "reference":
    modules.append("backprop")
for name in modules:
    cls = getattr(importlib.import_module(name), "Plotly")
    result["aliases"][name + ".Plotly"] = {key: Path(cls(fig)._path).read_text() for key, fig in encoded_figures().items()}
print(json.dumps(result, sort_keys=True))
