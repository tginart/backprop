"""Actual pinned per-import encoding for finite animation and layout branches.

Architecture: installed pinned W&B/local SDK in isolated processes -> independent
root/data_types/concrete imports -> genuine option Figures -> frozen file hashes.
Main caller: test_optional_plotly_options_contract. Browser rendering has a
separate driver so encoding equality cannot substitute for a frame outcome.
"""
import hashlib
import importlib
import inspect
import json
from pathlib import Path
import sys

from plotly_gap_options_figures import option_figures

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
figures = option_figures()
for module in ["wandb", "wandb.data_types", "wandb.sdk.data_types.plotly"]:
    cls = getattr(importlib.import_module(module), "Plotly")
    out["aliases"][module + ".Plotly"] = {name: {
        "bytes": Path(value._path).read_text(), "sha256": hashlib.sha256(Path(value._path).read_bytes()).hexdigest()
    } for name, figure in figures.items() for value in [cls(figure)]}
print(json.dumps(out, sort_keys=True))
