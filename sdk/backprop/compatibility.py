"""Install deliberate W&B module aliases for the implemented logging surface.

Architecture: top-level alias -> explicit namespace packages -> real Backprop
modules/classes. No source is imported twice under a second module name, so Run
classes and active globals retain one identity. Training integrations not supplied
by Backprop continue to fail at their exact missing import instead of being faked.
Main callers: sdk/wandb shim and the scoped backprop run launcher.
"""
from __future__ import annotations

import importlib
from importlib.machinery import ModuleSpec
import sys
from types import ModuleType


def _package(name):
    module = sys.modules.get(name)
    if module is None:
        module = ModuleType(name)
        module.__path__ = []
        module.__spec__ = ModuleSpec(name, loader=None, is_package=True)
        sys.modules[name] = module
    return module


def _alias(name, module):
    sys.modules[name] = module
    parent, _, child = name.rpartition(".")
    if parent and parent in sys.modules:
        setattr(sys.modules[parent], child, module)


def install_wandb_aliases():
    """Register only implemented modules, preserving shared class/global identity.

    Main callers: import wandb from this repository and python -m backprop run.
    """
    backprop = importlib.import_module("backprop")
    sys.modules["wandb"] = backprop
    for name in ["wandb.sdk", "wandb.sdk.lib", "wandb.sdk.data_types", "wandb.sdk.data_types.base_types",
                 "wandb.sdk.artifacts", "wandb.sdk.interface", "wandb.apis"]:
        module = _package(name)
        parent, _, child = name.rpartition(".")
        if parent:
            setattr(sys.modules[parent], child, module)
    for suffix, target in {
        "errors": "errors", "sdk.wandb_run": "run", "sdk.wandb_config": "config",
        "sdk.wandb_summary": "summary", "sdk.wandb_settings": "settings", "sdk.wandb_metric": "metrics",
        "plot": "plot", "data_types": "data_types", "sdk.lib.config_util": "config",
        "sdk.lib.run_moment": "branching",
        "sdk.lib.settings_file": "settings_files",
        "sdk.interface.summary_record": "summary",
        "sdk.data_types._dtypes": "table_types",
        "sdk.data_types.graph": "graph",
        "sdk.data_types.base_types.wb_value": "values", "sdk.data_types.base_types.media": "media",
        "sdk.artifacts.artifact": "artifacts", "sdk.artifacts.artifact_manifest_entry": "artifacts",
        "sdk.artifacts.artifact_manifest": "artifacts",
        "sdk.artifacts.exceptions": "artifact_identity",
    }.items():
        _alias("wandb." + suffix, importlib.import_module("backprop." + target))
    sdk = sys.modules["wandb.sdk"]
    for name in ["Run", "Settings", "Config", "Artifact", "init", "setup", "teardown", "finish", "login", "require"]:
        setattr(sdk, name, getattr(backprop, name))
    types = importlib.import_module("backprop.data_types")
    for name in ["Table", "Histogram", "Image", "Audio", "Video", "Html", "Plotly", "Artifact", "Classes"]:
        setattr(sys.modules["wandb.sdk.data_types"], name, getattr(types, name))
        module = ModuleType("wandb.sdk.data_types." + name.lower())
        setattr(module, name, getattr(types, name))
        _alias(module.__name__, module)
    for suffix, name in [("bounding_boxes_2d", "BoundingBoxes2D"), ("image_mask", "ImageMask")]:
        module = ModuleType("wandb.sdk.data_types." + suffix)
        setattr(module, name, getattr(types, name))
        _alias(module.__name__, module)
    for name, suffix in [("JoinedTable", "table"), ("EvalTable", "eval_table")]:
        cls = getattr(backprop, name)
        setattr(sys.modules["wandb.sdk.data_types"], name, cls)
        module_name = "wandb.sdk.data_types." + suffix
        if module_name not in sys.modules: _alias(module_name, ModuleType(module_name))
        setattr(sys.modules[module_name], name, cls)
    for name in ("Graph", "Node", "Edge"):
        setattr(sys.modules["wandb.sdk.data_types"], name, getattr(types, name))
    partitioned = importlib.import_module("backprop.partitioned_table")
    sys.modules["wandb.sdk.data_types"].PartitionedTable = partitioned.PartitionedTable
    sys.modules["wandb.sdk.data_types.table"].PartitionedTable = partitioned.PartitionedTable
    sys.modules["wandb.sdk.data_types.table"]._PartitionedTableType = partitioned._PartitionedTableType
    native_types = importlib.import_module("backprop.table_native_types")
    for suffix, names in {
        "table": ["_PrimaryKeyType", "_ForeignKeyType", "_ForeignIndexType", "_TableType"],
        "audio": ["_AudioFileType"], "video": ["_VideoFileType"], "html": ["_HtmlFileType"],
    }.items():
        for name in names:
            setattr(sys.modules["wandb.sdk.data_types." + suffix], name, getattr(native_types, name))
    native_tables = importlib.import_module("backprop.tables")
    for alias, name in [("_TableKey", "TableKey"), ("_TableIndex", "TableIndex"), ("_TableLinkMixin", "TableLinkMixin")]:
        setattr(sys.modules["wandb.sdk.data_types.table"], alias, getattr(native_tables, name))
    sys.modules["wandb.sdk.data_types.image"]._ImageFileType = importlib.import_module("backprop.table_image_type")._ImageFileType
    helper_types = _package("wandb.sdk.data_types.helper_types")
    _alias(helper_types.__name__, helper_types)
    for suffix in ["bounding_boxes_2d", "image_mask"]:
        _alias("wandb.sdk.data_types.helper_types." + suffix, sys.modules["wandb.sdk.data_types." + suffix])
    classes_module = ModuleType("wandb.sdk.data_types.helper_types.classes")
    classes_module.Classes, classes_module._ClassesIdType = types.Classes, native_types._ClassesIdType
    _alias(classes_module.__name__, classes_module)
    artifacts = importlib.import_module("backprop.artifacts")
    for name in ["Artifact", "ArtifactManifest", "ArtifactManifestEntry", "ArtifactDigestAlgorithm"]:
        setattr(sys.modules["wandb.sdk.artifacts"], name, getattr(artifacts, name))
    generated = _package("wandb.sdk.artifacts._generated")
    _alias(generated.__name__, generated)
    generated.ArtifactDigestAlgorithm = artifacts.ArtifactDigestAlgorithm
    enums = ModuleType("wandb.sdk.artifacts._generated.enums")
    enums.ArtifactDigestAlgorithm = artifacts.ArtifactDigestAlgorithm
    _alias(enums.__name__, enums)
    sys.modules["wandb.sdk.data_types.base_types"].WBValue = importlib.import_module("backprop.values").WBValue
    for name in ("Media", "BatchableMedia"):
        setattr(sys.modules["wandb.sdk.data_types.base_types"], name, getattr(types, name))
    helper = importlib.import_module("backprop.helper")
    _alias("wandb.sdk.wandb_helper", helper)
    backprop.helper = helper
    term = ModuleType("wandb.errors.term")
    for name in ["termsetup", "termlog", "termwarn", "termerror"]:
        setattr(term, name, getattr(backprop, name))
    _alias(term.__name__, term)
    try:
        public = importlib.import_module("backprop.apis.public")
    except ModuleNotFoundError as error:
        if error.name not in {"backprop.apis", "backprop.apis.public"}:
            raise
    else:
        _alias("wandb.apis", importlib.import_module("backprop.apis"))
        _alias("wandb.apis.public", public)
        backprop.Api = public.Api
        for name in ["api", "runs", "projects", "files", "artifacts", "history", "query", "replay", "runhistory", "runhistory.downloads"]:
            _alias("wandb.apis.public." + name, importlib.import_module("backprop.apis.public." + name))
    from .legacy_charts import Visualize
    backprop.plot.Visualize = Visualize
    chart_module = ModuleType("wandb.plot.custom_chart")
    for name in ("CustomChart", "CustomChartSpec", "plot_table"):
        setattr(chart_module, name, getattr(backprop.plot, name))
    _alias(chart_module.__name__, chart_module)
    from .legacy_charts import VisualizeSpec
    viz_module = ModuleType("wandb.plot.viz")
    viz_module.VisualizeSpec, viz_module.Visualize, viz_module.visualize = VisualizeSpec, Visualize, backprop.visualize
    _alias(viz_module.__name__, viz_module)
    from .integration_aliases import install
    install()
    from .unsupported import module_getattr
    module_getattr(sys.modules["wandb.sdk.data_types"], "wandb.data_types")
    return backprop
