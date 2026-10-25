"""W&B-shaped logging over a configured backend with stable process-local import aliases.

Architecture: setup resolves process defaults -> init chooses an active-run
policy -> each Run owns persistence -> finish removes global bindings. Public
helpers stay stable functions so imports made before init work for later runs.
Used by training programs, notebooks and the process-scoped launch CLI.
"""
from __future__ import annotations

import atexit
from datetime import timedelta
import os
import sys
import warnings
from pathlib import Path

from . import plot, helper
from .data_types import Artifact, Audio, Classes, Graph, Histogram, Html, Image, Node, Plotly, Table, Video
from .extended_tables import JoinedTable, EvalTable
from .legacy_charts import Visualize as _Visualize, visualize
plot.visualize = visualize
plot.Visualize = _Visualize
from .errors import Error, UsageError, CommError, AuthenticationError, ConfigError, UnsupportedError, UnsupportedFeatureError
from .config import Config
from .settings import Settings, in_notebook
from .run_calls import run_function as _run_function
from .run import Run, resolve_settings
from .transport import BackpropError, MemorySink, load_config, native_store, sync

__version__ = "0.1.1"
plot_table = plot.plot_table
run = None
_runs = []
_setup_settings = None
_previous_exception_hook = None
_previous_exit = None
_UNSET = object()
_term_settings = None
_term_logger = None
_term_seen = set()


def _uncaught_exception(kind, value, traceback):
    for active in _runs:
        if not active._closed:
            active._unhandled_exit_code = 1
    _previous_exception_hook(kind, value, traceback)


def _record_exit(code=0):
    value = code if isinstance(code, int) else (0 if code is None else 1)
    for active in _runs:
        if not active._closed:
            active._unhandled_exit_code = value
    return _previous_exit(code)


def _install_exit_hooks():
    global _previous_exception_hook, _previous_exit
    if _previous_exception_hook is None:
        _previous_exception_hook = sys.excepthook
        sys.excepthook = _uncaught_exception
    if _previous_exit is None:
        _previous_exit = sys.exit
        sys.exit = _record_exit


def init(entity=None, project=None, dir=None, id=None, name=None, notes=None, tags=None,
         config=None, config_exclude_keys=None, config_include_keys=None, allow_val_change=None,
         group=None, job_type=None, mode=None, force=None, reinit=None, resume=None,
         resume_from=None, fork_from=None, save_code=None, tensorboard=None,
         sync_tensorboard=None, monitor_gym=None, settings=None, anonymous=_UNSET,
         *, _sink=None, _deployment=None, _shared_writer=False, _event_time_floor=None):
    """Validate arguments, apply reinitialization policy, then create one Run.

    Main callers: training entry points and the unchanged wandb import shim.
    Active create_new runs participate in return_previous/finish_previous even
    though they intentionally leave the module's global run binding unchanged.
    """
    global run
    from .config import parse_config
    choices = {"reinit": reinit, "resume": resume, "mode": mode, "force": force,
               "allow_val_change": allow_val_change, "save_code": save_code,
               "sync_tensorboard": sync_tensorboard if sync_tensorboard is not None else tensorboard,
               "project": project, "entity": entity, "root_dir": dir, "run_id": id,
               "run_name": name, "run_notes": notes,
               "run_tags": tuple(tags) if tags is not None else None,
               "run_group": group, "run_job_type": job_type,
               "resume_from": resume_from, "fork_from": fork_from}
    # Explicit init arguments update a caller-supplied Settings object in the
    # pinned SDK. Setup defaults are a separate lifecycle and remain untouched.
    if isinstance(settings, Settings):
        settings.update_from_dict({k: v for k, v in choices.items() if v is not None})
        if anonymous is not _UNSET:
            settings.anonymous = anonymous
    selected = resolve_settings(settings)
    selected.update_from_dict({k: v for k, v in choices.items() if v is not None})
    if anonymous is not _UNSET:
        selected.anonymous = anonymous
    if sum(x is not None for x in [selected.resume, selected.resume_from, selected.fork_from]) > 1:
        raise UsageError("resume, resume_from and fork_from are mutually exclusive")
    if resume_from or fork_from:
        # Branching is handled by Run initialization, with a consistent source
        # history snapshot, before any new event is written.
        selected.update_from_dict({"resume_from": resume_from, "fork_from": fork_from})
    prepared_auth = None
    if _sink is None and selected.mode in {"online", "shared", "run"}:
        from backprop_backend.connection import prepare_init_candidate
        from importlib import import_module
        runtime = import_module('.run', __name__)
        _deployment, prepared_auth = prepare_init_candidate(selected, settings, _deployment,
            load_config=runtime.load_config, native_store=runtime.native_store, setup=_setup_settings)
    parsed_config = parse_config(config, config_exclude_keys, config_include_keys)
    from .init_integrations import prepared_integrations
    with prepared_integrations(selected, monitor_gym):
        return _start_selected_run(selected, project=project, entity=entity, name=name,
            id=id, config=parsed_config, tags=choices['run_tags'],
            group=group, job_type=job_type,
            notes=notes, dir=dir, _sink=_sink, _deployment=_deployment,
            _shared_writer=_shared_writer, _event_time_floor=_event_time_floor,
            _prepared_auth=prepared_auth)


def _start_selected_run(selected, **options):
    """Choose a handle after argument and integration validation succeeds.

    Main caller: init, while its temporary hook lease is alive. Keeping this
    stage separate prevents failed adapter setup from closing an existing run.
    """
    global run
    from .init_integrations import prepare_run_defaults
    selected = prepare_run_defaults(selected)
    if selected.mode == 'disabled':
        # Public arguments already reached Settings. The disabled copy owns
        # its derived defaults, including the temporary root and dummy project.
        for field in ['project', 'entity', 'name', 'id', 'dir']:
            options[field] = None
    _runs[:] = [item for item in _runs if not item._closed]
    active = [item for item in _runs if not item.disabled]
    behavior = selected.reinit
    if behavior == "default":
        behavior = "finish_previous" if in_notebook() else "return_previous"
    if behavior is True:
        behavior = "finish_previous"
    elif behavior is False:
        behavior = "return_previous"
    if selected.mode == "disabled":
        # A disabled handle is fresh and globally bound under every policy.
        # It never finishes or returns an existing live handle.
        active = []
    if active and behavior == "finish_previous":
        for previous in list(active):
            previous.finish()
    elif active and behavior != "create_new":
        if selected.resume == "must":
            raise Error("Cannot resume a run while another run is active."
                        " You must either finish it using run.finish(),"
                        " or use reinit='create_new' when calling wandb.init().")
        if selected.sync_tensorboard:
            from .init_integrations import adopt_tensorboard
            adopt_tensorboard(active[-1])
        return active[-1]
    options['_tensorboard_nonglobal_init'] = behavior == 'create_new' and selected.mode != 'disabled'
    created = _construct_init_run(selected, **options)
    _install_exit_hooks()
    _runs.append(created)
    if created.disabled or behavior != "create_new":
        run = created
    if not created.disabled and selected.save_code and not selected.disable_code:
        if selected.code_dir:
            created.log_code(selected.code_dir)
        elif getattr(created._settings, "_notebook_path", None) or getattr(created._settings, "_notebook_provider", None):
            created._notebook.refresh()
        elif not selected.notebook_name and selected.program and Path(selected.program).is_file():
            program = Path(selected.program).resolve()
            created.log_code(program.parent, include_fn=lambda path: Path(path) == program)
    return created


def _construct_init_run(settings, **options):
    """Keep init's public communication errors separate from journal failures.

    Main caller: _start_selected_run after the chosen reinit policy. Direct Run
    construction keeps its native errors.
    Why this exists: the journal preserves retry diagnostics in BackpropError,
    while the pinned public init reports backend HTTP failures as CommError.
    Preserve the original wrapper and Foundry cause without adding response or
    credential text; user/config errors retain their original exception.
    """
    try:
        return Run(settings=settings, **options)
    except Exception as error:
        if isinstance(error, TimeoutError) and getattr(error, '_backprop_init_transport_timeout', False):
            message = (f'Timed out initializing run: {error}\n'
                       'Consider increasing the init_timeout setting:\n'
                       '  wandb.init(settings=wandb.Settings(init_timeout=...))')
            raise CommError(message, exc=error) from error
        from backprop_backend.http_store import BackendError
        cause, seen = error, set()
        while cause is not None and id(cause) not in seen:
            if isinstance(cause, BackendError):
                raise CommError(str(error), exc=error) from error
            seen.add(id(cause))
            cause = cause.__cause__
        raise


def _active():
    if run is None or run._closed:
        raise Error("You must call wandb.init() before using this logging API")
    return run


def _on_finish(finished):
    global run
    if run is finished:
        run = None
    _runs[:] = [item for item in _runs if item is not finished and not item._closed]


def log(data, step=None, commit=None):
    return _active().log(data, step=step, commit=commit)


def watch(models, criterion=None, log="gradients", log_freq=1000, idx=None, log_graph=False):
    return _active().watch(models, criterion, log, log_freq, idx, log_graph)


def unwatch(models=None):
    return _active().unwatch(models)


def define_metric(name, step_metric=None, step_sync=None, hidden=None, summary=None, overwrite=None):
    return _active().define_metric(name, step_metric, step_sync, hidden, summary, overwrite)


log_artifact = _run_function(Run.log_artifact)


def finish(exit_code=None, quiet=None):
    if run is not None:
        run.finish(exit_code=exit_code, quiet=quiet)


join = finish


def setup(settings=None):
    """Set process defaults without opening a run or starting a network service."""
    global _setup_settings
    _setup_settings = resolve_settings(settings)
    values = settings._explicit if isinstance(settings, Settings) else (settings or {})
    if 'api_key' in values and _setup_settings.api_key:
        from backprop_backend.connection import clear_init_session
        clear_init_session()
    return _Setup(_setup_settings)


class _Setup:
    def __init__(self, settings):
        self._settings = settings

    settings = property(lambda self: self._settings.model_copy(deep=True))


def _publish_login_defaults(defaults):
    """Publish validated login defaults without disconnecting setup handles.

    Main callers: login and implicit Api after verification/persistence, and
    init after auth selection before its separate backend-startup stage.
    Why this exists: setup returns a handle to these process-owned settings;
    replacing the settings object leaves an earlier handle with obsolete auth.
    Update that owned object while caller-supplied Settings remain detached.
    Teardown clears the global owner, so retained handles keep their last state.
    """
    global _setup_settings
    if _setup_settings is None:
        _setup_settings = defaults
    else:
        _setup_settings.update_from_settings(defaults)


def teardown(exit_code=None):
    global _setup_settings
    for active in list(_runs):
        try:
            active.finish(exit_code=exit_code)
        except TimeoutError:
            # Official teardown honors the timeout but deliberately suppresses
            # finish_timeout_raises, because teardown also runs during atexit.
            pass
    _setup_settings = None
    from backprop_backend.connection import clear_init_session
    clear_init_session()
    gym_module = sys.modules.get("backprop.integration.gym")
    if gym_module is not None:
        gym_module._unpatch()


def login(key=None, relogin=None, host=None, force=None, timeout=None, verify=True,
          referrer=None, prompt=True, anonymous=_UNSET):
    """Select backend credentials and persist explicit or prompted tokens.

    Main callers: SDK clients before init. Verification precedes host-scoped
    private-cache and session publication. W&B credential files are not used.
    """
    global _setup_settings
    if anonymous is not _UNSET:
        warnings.warn("The anonymous login parameter has no effect and is deprecated", DeprecationWarning, stacklevel=2)
    if run is not None:
        termwarn("Calling login after init has no effect")
        return False
    selected = resolve_settings()
    if selected.mode in {"disabled", "offline", "dryrun"}:
        return False
    from backprop_backend.connection import authenticate_candidate, persist_candidate, remember_init_session, configure_settings, publish_candidate
    candidate = authenticate_candidate(key=key, host=host, prompt=prompt, verify=verify,
                                       timeout=timeout, settings=selected, relogin=relogin, force=force, referrer=referrer,
                                       load_config=load_config, native_store=native_store)
    if candidate is None:
        return False
    # Validate a detached copy before publishing either process defaults or
    # environment credentials. Explicit per-run settings still win at init.
    defaults = _setup_settings.model_copy(deep=True) if _setup_settings is not None else Settings()
    if candidate.mode != 'online':
        defaults.mode = candidate.mode
        _publish_login_defaults(defaults)
        return False
    configure_settings(defaults, candidate)
    persist_candidate(candidate, selected)
    publish_candidate(candidate)
    _publish_login_defaults(defaults)
    remember_init_session(candidate)
    return True


def save(glob_str, base_path=None, policy="live", glob=True):
    return _active().save(glob_str, base_path, policy, glob)


def restore(name, run_path=None, replace=False, root=None):
    if run is not None:
        return run.restore(name, run_path, replace, root)
    if not run_path:
        raise UsageError("run_path is required when no run is active")
    from .file_logging import restore_file
    entity, project, run_id = run_path.rsplit("/", 2)
    store = native_store(load_config())
    return restore_file(store, store.read_events(project=project, run_id=run_id, entity=entity), name, replace=replace, root=root)


use_artifact = _run_function(Run.use_artifact)


log_model = _run_function(Run.log_model)


use_model = _run_function(Run.use_model)


def pin_config_keys(keys=()):
    return _active().pin_config_keys(keys)


def mark_preempting():
    return _active().mark_preempting()


def _terminal_message(string, level, newline, repeat, prefix):
    settings = run._settings if run is not None else (_term_settings or _setup_settings)
    if level == "warnings" and run is not None:
        run._warnings.append(str(string))
    if settings and (settings.silent or not getattr(settings, "show_"+level)
                     or (level == "info" and settings.quiet)):
        return
    text = str(string)
    if not repeat and text in _term_seen:
        return
    _term_seen.add(text)
    color_preference = settings.show_colors if settings else None
    if color_preference is False or (color_preference is None and not getattr(sys.stderr, "isatty", lambda: False)()):
        import re
        text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    label = {"info": "", "warnings": "WARNING ", "errors": "ERROR "}[level]
    print(("wandb: " if prefix else "") + label + text, end="\n" if newline else "", file=sys.stderr)
    if _term_logger:
        import logging
        _term_logger.log({"info": logging.INFO, "warnings": logging.WARNING, "errors": logging.ERROR}[level], text)


def termlog(string="", newline=True, repeat=True, prefix=True):
    return _terminal_message(string, "info", newline, repeat, prefix)


def termwarn(string, newline=True, repeat=True, prefix=True):
    return _terminal_message(string, "warnings", newline, repeat, prefix)


def termerror(string, newline=True, repeat=True, prefix=True):
    return _terminal_message(string, "errors", newline, repeat, prefix)


def termsetup(settings=None, logger=None):
    global _term_settings, _term_logger
    if settings is not None:
        _term_settings = settings if isinstance(settings, Settings) else Settings(**settings)
        from .settings_policy import presentation_diagnostics
        for message in presentation_diagnostics(_term_settings):
            if not _term_settings.silent and _term_settings.show_warnings:
                warnings.warn(message, RuntimeWarning, stacklevel=2)
    _term_logger = logger


def require(requirement=None, experiment=None):
    from .feature_require import apply_requirements
    return apply_requirements(requirement, experiment, termwarn)


def ensure_configured():
    """Emit the pinned deprecation warning without initializing a run or setup.

    Main callers: older training code that checked configuration before login.
    Credentials are resolved by login/Api when used, not by this deprecated call.
    """
    termwarn("wandb.api and wandb.ensure_configured() are deprecated and will be removed"
             " in a future release. To check for credentials without prompting,"
             " use wandb.login(prompt=False).", repeat=False)


def set_trace():
    import pdb
    pdb.set_trace()


class _PreInitObject:
    def __init__(self, name):
        object.__setattr__(self, "_name", name)

    def __getitem__(self, key):
        raise Error(f"You must call wandb.init() before {self._name}[{key!r}]")

    def __setitem__(self, key, value):
        self[key]

    def __getattr__(self, key):
        if key.startswith("_"):
            raise AttributeError(key)
        raise Error(f"You must call wandb.init() before {self._name}.{key}")

    def __setattr__(self, key, value):
        if key.startswith("_"):
            object.__setattr__(self, key, value)
        else:
            self[key]


_preinit = {name: _PreInitObject("wandb." + name) for name in ["config", "summary"]}
# Importing the focused modules gives the package module-valued attributes.
# Public wandb.config/summary must instead resolve to the active run's objects.
globals().pop("config", None)
globals().pop("summary", None)


def __getattr__(name):
    if name in {"keras", "lightgbm", "xgboost", "catboost", "sklearn", "fastai", "tensorboard", "gym"}:
        import importlib
        return importlib.import_module("backprop.integration." + name)
    if name in {"config", "summary"}:
        return getattr(run, name) if run is not None and not run._closed else _preinit[name]
    if name == "Api":
        from .apis.public import Api
        return Api
    if name in {"sweep", "agent", "controller", "watch", "unwatch", "use_artifact", "alert"}:
        raise NotImplementedError(f"wandb.{name} is outside the current logging implementation")
    raise AttributeError(name)


def _finish_at_exit():
    for active in list(_runs):
        if not active._closed:
            try:
                active.finish(exit_code=active._unhandled_exit_code)
            except Exception as error:
                warnings.warn(f"Backprop could not finish run {active.id}; data remains in {active.sync_dir}: {error}", RuntimeWarning)


atexit.register(_finish_at_exit)
__all__ = ["init", "log", "finish", "join", "setup", "teardown", "define_metric", "log_artifact", "login", "sync",
           "save", "restore", "use_artifact", "log_model", "use_model", "pin_config_keys", "mark_preempting",
           "run", "config", "summary", "Api", "Run", "Settings", "Config", "Error", "UsageError", "CommError", "AuthenticationError", "UnsupportedError", "UnsupportedFeatureError",
           "Table", "Histogram", "Image", "Audio", "Video", "Html", "Plotly", "Artifact", "Classes", "plot", "plot_table", "BackpropError", "MemorySink"]

# Install rejection boundaries after the supported top-level exports exist.
from .unsupported import install as _install_unsupported_guards
_install_unsupported_guards()
