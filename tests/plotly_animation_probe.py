"""Independent alias bytes for this finite animation batch.

Architecture: pinned/local process -> genuine Figure -> every public alias file.
Main caller: test_plotly_animation_encoding.py. Legacy reconstruction is kept in
the separate browser fixture rather than falsely attributed to Plotly Python.
"""
import hashlib
import importlib
import inspect
import json
from pathlib import Path
import sys
from plotly_animation_fixtures import genuine_figures

if sys.argv[1] == "reference":
    import wandb as w
    assert w.__version__ == "0.30.0"
else:
    import backprop as w
    from backprop.compatibility import install_wandb_aliases
    install_wandb_aliases()
out = {"aliases": {}}
if sys.argv[1] == "reference":
    out["source_sha256"] = hashlib.sha256(Path(inspect.getfile(w.Plotly)).read_bytes()).hexdigest()
for module in ["wandb", "wandb.data_types", "wandb.sdk.data_types.plotly"]:
    cls = getattr(importlib.import_module(module), "Plotly")
    out["aliases"][module + ".Plotly"] = {name: Path(cls(fig)._path).read_text() for name, fig in genuine_figures().items()}
print(json.dumps(out, sort_keys=True))
