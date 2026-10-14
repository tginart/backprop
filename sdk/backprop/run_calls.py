"""Logging helper lifecycle and stable module-level dispatch.

Architecture: module function selects the active run -> lifecycle gate runs
before argument binding -> the logging operation uses ordinary artifact storage.
Main callers: Run artifact/model methods and their module-level exports.
Why this exists: disabled and finished runs have observable behavior even when
arguments are invalid; stable imports must also work across repeated sessions.
"""
from functools import wraps
import inspect

from .errors import Error, UsageError


def run_method(method=None, *, disabled_result=None):
    """Apply disabled/finished behavior before binding operation arguments.

    Main callers: the artifact/model logging and input methods. A factory can
    supply the disabled artifact Noop. Each operation keeps its full signature; no arguments disappear into the wrapper.
    """
    if method is None:
        return lambda selected: run_method(selected, disabled_result=disabled_result)

    @wraps(method)
    def call(run, *args, **kwargs):
        if run.disabled:
            return disabled_result() if disabled_result is not None else None
        if run._closed:
            raise UsageError(
                f"Run ({run.id}) is finished. The call to"
                f" `{method.__name__}` will be ignored."
                " Please make sure that you are using an active run."
            )
        run._check()
        return method(run, *args, **kwargs)
    return call


def run_function(method):
    """Build a stable public function that forwards to the current run method.

    Main caller: the package's artifact/model exports. W&B rebinds its module function
    at initialization; this Foundry equivalent keeps pre-imported functions
    useful in later sessions while retaining method validation and signatures.
    """
    @wraps(method)
    def call(*args, **kwargs):
        from . import run
        if run is None:
            raise Error(f"You must call wandb.init() before wandb.{method.__name__}()")
        return getattr(run, method.__name__)(*args, **kwargs)
    signature = inspect.signature(method)
    call.__signature__ = signature.replace(parameters=list(signature.parameters.values())[1:])
    call.__qualname__ = method.__name__
    return call
