"""Local import and value predicates for the vendored pure dtype engine.

Architecture: optional libraries are imported only on request; artifact strings
are recognized as data, with no authentication or service access. The helpers
retain the pinned predicates so the type algorithms can remain unchanged.
Main callers: table_types. No helper installs packages or imports logged code.
"""
import importlib


def get_module(name):
    """Mirror the pinned nonrequired optional-library import boundary."""
    try:
        return importlib.import_module(name)
    except Exception:
        return None


def is_numpy_array(value):
    # Like pinned util.is_numpy_array, include ndarray subclasses. Importlib's
    # module cache avoids repeatedly loading the optional dependency.
    numpy = get_module("numpy")
    return numpy is not None and isinstance(value, numpy.ndarray)


def _is_artifact_string(value):
    return isinstance(value, str) and value.startswith("wandb-artifact://")


def _is_artifact_version_weave_dict(value):
    return isinstance(value, dict) and value.get("_type") == "artifactVersion"
