"""Isolated pinned/native public signatures, aliases, errors and sync boundary.

Architecture: import exactly one SDK -> inspect each public alias separately ->
call patch/unpatch and the selected switches -> observe the public sync return.
Main caller: test_optional_tensorboard_gaps. Only W&B's hosted SyncManager and
Api.run lookup are replaced for the API test; the real selected method runs.
"""
from __future__ import annotations

import importlib
import inspect
import json
import os
from pathlib import Path
import sys
import tempfile
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

REFERENCE = sys.argv[1] == "reference"
if REFERENCE:
    import wandb as sdk
    assert sdk.__version__ == "0.30.0"
else:
    import backprop as sdk
    from backprop.compatibility import install_wandb_aliases
    install_wandb_aliases()

from wandb import tensorboard as public
canonical = importlib.import_module("wandb.integration.tensorboard")


def attempt(function):
    try:
        result = function()
        return {"value": result}
    except Exception as error:
        return {"error": "Error" if type(error) is sdk.Error else type(error).__name__, "message": str(error)}


def signature(function):
    return [{"name": value.name, "default": None if value.default is inspect.Parameter.empty else value.default,
             "required": value.default is inspect.Parameter.empty, "kind": value.kind.name}
            for value in inspect.signature(function).parameters.values()]


out = {"version": sdk.__version__, "signatures": {}, "aliases": {}}
for label, module in (("wandb.tensorboard", public), ("wandb.integration.tensorboard", canonical)):
    for name in ("patch", "unpatch"):
        out["signatures"][label + "." + name] = signature(getattr(module, name))
        out["aliases"][label + "." + name] = getattr(module, name) is getattr(canonical, name)
out["signatures"]["wandb.Api.sync_tensorboard"] = signature(sdk.Api.sync_tensorboard)
public.unpatch()
out["empty_unpatch"] = attempt(public.unpatch)
try:
    import tensorboard
except ImportError:
    out["missing_tensorboard"] = attempt(public.patch)
else:
    original = importlib.import_module("tensorboard.summary.writer.event_file_writer").EventFileWriter
    original_init = original.__init__
    out["patch"] = attempt(lambda: public.patch(save=False, tensorboard_x=False, pytorch=False))
    out["double_patch"] = attempt(public.patch)
    public.unpatch()
    restored = importlib.import_module("tensorboard.summary.writer.event_file_writer").EventFileWriter
    out["restored"] = restored is original and restored.__init__ is original_init
    out["switches"] = []
    for pytorch, tensorboard_x in ((True, None), (None, True), (False, False), (True, True)):
        out["switches"].append(attempt(lambda: public.patch(save=True, pytorch=pytorch, tensorboard_x=tensorboard_x)))
        public.unpatch()

if REFERENCE:
    calls = {}

    class ServiceBoundary:
        """Capture the unavoidable hosted sync boundary without faking SDK logic."""
        def __init__(self, **kwargs):
            calls["options"] = kwargs
        def add(self, path):
            calls["path"] = path
        def start(self):
            calls["started"] = True
        def is_done(self):
            return True

    service = SimpleNamespace(settings={"project": "p"}, default_entity="e",
                              _service_api=SimpleNamespace(app_url="https://reference.invalid"),
                              run=lambda path: {"run_path": path})
    out["sync_direct"] = attempt(lambda: sdk.Api.sync_tensorboard(service, "events", run_id="tb-import"))
    sync_module = ModuleType("wandb.sync")
    sync_module.SyncManager = ServiceBoundary
    with patch.dict(sys.modules, {"wandb.sync": sync_module}):
        out["sync_service_boundary"] = sdk.Api.sync_tensorboard(service, "events", run_id="tb-import")
    out["sync_calls"] = calls
print(json.dumps(out, sort_keys=True))
