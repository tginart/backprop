"""Lazy import aliases preserve optional dependency isolation.

Architecture: whitelist supported wandb.integration paths -> import the actual
Backprop module once -> expose the same object at its compatibility path.
Main callers: install_wandb_aliases. Importing wandb alone never loads frameworks.
"""
import importlib
import importlib.abc
import importlib.util
import sys

_ALLOWED = {"keras", "lightning", "xgboost", "lightgbm", "catboost", "sklearn", "fastai", "sb3", "tensorboard", "gym"}


class IntegrationAliases(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def find_spec(self, fullname, path=None, target=None):
        parts = fullname.split('.')
        if parts[0] != 'wandb': return None
        if len(parts) >= 3 and parts[1] == 'integration' and parts[2] in _ALLOWED:
            native = 'backprop.' + '.'.join(parts[1:])
        elif len(parts) >= 2 and parts[1] in _ALLOWED:
            native = 'backprop.integration.' + '.'.join(parts[1:])
        else: return None
        spec = importlib.util.find_spec(native)
        if spec is None: return None
        result = importlib.util.spec_from_loader(fullname, self, is_package=spec.submodule_search_locations is not None)
        result.loader_state = native
        return result

    def create_module(self, spec): return importlib.import_module(spec.loader_state)
    def exec_module(self, module): pass


def install():
    import backprop
    integration = importlib.import_module('backprop.integration')
    sys.modules['wandb.integration'] = integration
    backprop.integration = integration
    if not any(isinstance(finder, IntegrationAliases) for finder in sys.meta_path):
        sys.meta_path.insert(0, IntegrationAliases())
