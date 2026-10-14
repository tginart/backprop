"""Reject unimplemented public contracts before any work or persistence.

Architecture: assessed missing catalog -> import/member guards -> UnsupportedError.
Supported and partial implementations are untouched. Unknown Python typos retain
AttributeError; known missing APIs name the rejected surface. No optional training
framework is imported by guard installation. Main callers: package initialization,
compatibility aliases, and the one mixed integration with an unavailable export.
"""
import importlib
import importlib.abc
import inspect
import sys
from types import ModuleType

from .errors import UnsupportedFeatureError
from .unsupported_catalog import BOUNDARIES


def reject(api):
    """Fail at the public boundary without inspecting credentials or arguments."""
    raise UnsupportedFeatureError(f"{api} is not implemented in Backprop. See docs/python-api-support.md.")


def unavailable(api):
    """Build a marked rejecting function, never a successful placeholder.

    Main caller: install. The audit recognizes the marker as missing behavior,
    so the existence of a rejection wrapper cannot turn a row green.
    """
    def fail(*args, **kwargs):
        reject(api)
    fail.__name__ = api.rsplit('.', 1)[-1]
    fail.__backprop_unsupported_api__ = api
    return fail


def module_getattr(module, api):
    """Add errors for known unavailable exports while keeping other lookups.

    Main callers: core installation and optional integration modules. Existing
    lazy imports and active-run config/summary dispatch keep their own behavior.
    """
    previous = vars(module).get('__getattr__')
    if getattr(previous, '__backprop_guard__', False):
        return
    def lookup(name):
        path = api+'.'+name
        if path in BOUNDARIES:
            reject(path)
        if previous is not None:
            return previous(name)
        raise AttributeError(f"module {module.__name__!r} has no attribute {name!r}")
    lookup.__backprop_guard__ = True
    lookup.__backprop_missing_exports__ = {path[len(api)+1:] for path in BOUNDARIES
        if path.startswith(api+'.') and '.' not in path[len(api)+1:]}
    module.__getattr__ = lookup


class UnavailableImports(importlib.abc.MetaPathFinder):
    """Reject wholly unsupported modules and their children without loading them."""
    def find_spec(self, fullname, path=None, target=None):
        api = 'wandb'+fullname[len('backprop'):] if fullname.startswith('backprop.') else fullname
        for boundary, kind in BOUNDARIES.items():
            if kind in {'module', 'namespace'} and (api == boundary or api.startswith(boundary+'.')):
                reject(api)
        # Launch is explicitly excluded but its implementation-specific children
        # are outside the captured logging API inventory.
        if api == 'wandb.sdk.launch' or api.startswith('wandb.sdk.launch.'):
            reject(api)
        return None


def install():
    """Guard missing members of real core classes, including all shared aliases.

    Main caller: backprop package initialization after its exports exist. Settings
    fields are checked when a value requests an unsupported effect; defaults that
    disable absent services remain usable. Repeated shim installation is harmless.
    """
    import backprop
    public = importlib.import_module('backprop.apis.public')
    modules = {'wandb': backprop, 'wandb.data_types': backprop.data_types,
               'wandb.apis.public': public, 'wandb.Api': public.Api}
    for api, kind in BOUNDARIES.items():
        if api.startswith('wandb.Settings.') and kind == 'field':
            continue
        prefix = max((name for name in modules if api.startswith(name+'.')), key=len)
        value = modules[prefix]
        parts = api[len(prefix)+1:].split('.')
        for part in parts[:-1]:
            try:
                value = inspect.getattr_static(value, part)
            except AttributeError:
                value = None
                break
        if not isinstance(value, type):
            continue
        name = parts[-1]
        fail = unavailable(api)
        if name == '__getattr__':
            # Python itself probes private hooks, including during copying.
            # Public attempts at the absent service-backed dynamic lookup fail.
            def dynamic(self, key, _api=api):
                if key.startswith('_'):
                    raise AttributeError(key)
                reject(_api)
            dynamic.__backprop_unsupported_api__ = api
            setattr(value, name, dynamic)
        elif kind in {'property', 'field'}:
            setattr(value, name, property(fail, fail, fail))
        else:
            setattr(value, name, fail)
    for api, module in modules.items():
        if isinstance(module, ModuleType):
            module_getattr(module, api)
    if not any(isinstance(finder, UnavailableImports) for finder in sys.meta_path):
        sys.meta_path.insert(0, UnavailableImports())
