"""Settings values, validation and deterministic source precedence.

Architecture: defaults -> settings files -> environment -> setup -> init values.
The model is dependency-free and returns detached copies for Run.settings.
Main callers: module setup/init and Run construction. Foundry connection details
remain in BACKPROP_CONFIG rather than being confused with W&B server URLs.
"""
from __future__ import annotations

import configparser
import copy
import json
import math
import os
from pathlib import Path
import platform
import socket
import sys
import time
import warnings
from urllib.parse import urlsplit, quote

from .errors import UsageError, UnsupportedError
from backprop_backend.connection import credentials_path as default_path
from .branching import RunMoment

_ANONYMOUS_UNSET = object()

_DEFAULTS = {'allow_media_symlink': False, 'allow_offline_artifacts': True, 'allow_val_change': False, 'anonymous': _ANONYMOUS_UNSET, 'api_key': None, 'azure_account_url_to_access_key': None, 'app_url_override': None, 'base_url': 'https://api.wandb.ai', 'code_dir': None, 'config_paths': None, 'console': 'auto', 'console_multipart': False, 'console_chunk_max_bytes': 0, 'console_chunk_max_seconds': 0, 'capture_loggers': None, 'credentials_file': None, 'disable_code': False, 'disable_git': False, 'disable_git_fork_point': True, 'disable_job_creation': True, 'docker': None, 'email': None, 'entity': None, 'organization': None, 'force': False, 'fork_from': None, 'git_commit': None, 'git_remote': 'origin', 'git_remote_url': None, 'git_root': None, 'heartbeat_seconds': 30, 'host': None, 'http_proxy': None, 'https_proxy': None, 'identity_token_file': None, 'ignore_globs': (), 'init_timeout': 90.0, 'finish_timeout': 0.0, 'finish_timeout_raises': False, 'insecure_disable_ssl': False, 'job_name': None, 'job_source': None, 'label_disable': False, 'launch': False, 'launch_config_path': None, 'login_timeout': None, 'mode': 'online', 'notebook_name': None, 'program': None, 'program_abspath': None, 'program_relpath': None, 'project': None, 'quiet': False, 'reinit': 'default', 'relogin': False, 'resume': None, 'resume_from': None, 'resumed': False, 'root_dir': None, 'run_group': None, 'run_id': None, 'run_job_type': None, 'run_name': None, 'run_notes': None, 'run_tags': None, 'sagemaker_disable': False, 'save_code': None, 'settings_system': None, 'stop_fn': None, 'max_end_of_run_history_metrics': 10, 'max_end_of_run_summary_metrics': 10, 'show_colors': None, 'show_emoji': None, 'show_errors': True, 'show_info': True, 'show_warnings': True, 'silent': False, 'stop_on_fatal_error': False, 'strict': None, 'summary_timeout': 60, 'summary_warnings': 5, 'sweep_id': None, 'sweep_param_path': None, 'symlink': None, 'sync_tensorboard': None, 'table_raise_on_max_row_limit_exceeded': False, 'use_dot_wandb': None, 'username': None}
_TYPES = {'allow_media_symlink': 'bool', 'allow_offline_artifacts': 'bool', 'allow_val_change': 'bool', 'anonymous': 'deprecation.DoNotSet', 'api_key': 'str | None', 'azure_account_url_to_access_key': 'dict[str, str] | None', 'app_url_override': 'str | None', 'base_url': 'str', 'code_dir': 'str | None', 'config_paths': 'Sequence[str] | None', 'console': "Literal['auto', 'off', 'wrap', 'redirect', 'wrap_raw', 'wrap_emu']", 'console_multipart': 'bool', 'console_chunk_max_bytes': 'int', 'console_chunk_max_seconds': 'int', 'capture_loggers': 'dict[str, str] | None', 'credentials_file': 'str', 'disable_code': 'bool', 'disable_git': 'bool', 'disable_git_fork_point': 'bool', 'disable_job_creation': 'bool', 'docker': 'str | None', 'email': 'str | None', 'entity': 'str | None', 'organization': 'str | None', 'force': 'bool', 'fork_from': 'RunMoment | None', 'git_commit': 'str | None', 'git_remote': 'str', 'git_remote_url': 'str | None', 'git_root': 'str | None', 'heartbeat_seconds': 'int', 'host': 'str | None', 'http_proxy': 'str | None', 'https_proxy': 'str | None', 'identity_token_file': 'str | None', 'ignore_globs': 'Sequence[str]', 'init_timeout': 'float', 'finish_timeout': 'float', 'finish_timeout_raises': 'bool', 'insecure_disable_ssl': 'bool', 'job_name': 'str | None', 'job_source': "Literal['repo', 'artifact', 'image'] | None", 'label_disable': 'bool', 'launch': 'bool', 'launch_config_path': 'str | None', 'login_timeout': 'float | None', 'mode': "Literal['online', 'offline', 'shared', 'disabled', 'dryrun', 'run']", 'notebook_name': 'str | None', 'program': 'str | None', 'program_abspath': 'str | None', 'program_relpath': 'str | None', 'project': 'str | None', 'quiet': 'bool', 'reinit': "Literal['default', 'return_previous', 'finish_previous', 'create_new'] | bool", 'relogin': 'bool', 'resume': "Literal['allow', 'must', 'never', 'auto'] | None", 'resume_from': 'RunMoment | None', 'resumed': 'bool', 'root_dir': 'str', 'run_group': 'str | None', 'run_id': 'str | None', 'run_job_type': 'str | None', 'run_name': 'str | None', 'run_notes': 'str | None', 'run_tags': 'tuple[str, ...] | None', 'sagemaker_disable': 'bool', 'save_code': 'bool | None', 'settings_system': 'str | None', 'stop_fn': 'Callable[[], None] | None', 'max_end_of_run_history_metrics': 'int', 'max_end_of_run_summary_metrics': 'int', 'show_colors': 'bool | None', 'show_emoji': 'bool | None', 'show_errors': 'bool', 'show_info': 'bool', 'show_warnings': 'bool', 'silent': 'bool', 'stop_on_fatal_error': 'bool', 'strict': 'bool | None', 'summary_timeout': 'int', 'summary_warnings': 'int', 'sweep_id': 'str | None', 'sweep_param_path': 'str | None', 'symlink': 'bool', 'sync_tensorboard': 'bool | None', 'table_raise_on_max_row_limit_exceeded': 'bool', 'use_dot_wandb': 'bool | None', 'username': 'str | None'}

_DEFAULTS.update(x_label=None, x_primary=True, x_update_finish_state=True,
                 x_disable_stats=False, x_stats_sampling_interval=15.0)
_TYPES.update(x_label="str | None", x_primary="bool", x_update_finish_state="bool",
              x_disable_stats="bool", x_stats_sampling_interval="float")


def in_notebook():
    """Detect a notebook shell without importing notebook dependencies."""
    shell = sys.modules.get("IPython")
    return bool(shell and getattr(shell, "get_ipython", lambda: None)() is not None)


def in_jupyter():
    shell = sys.modules.get("IPython")
    instance = getattr(shell, "get_ipython", lambda: None)() if shell else None
    if instance is None or "terminal" in type(instance).__module__ or "spyder" in sys.modules:
        return False
    config = getattr(instance, "config", {})
    connection = ((config.get("IPKernelApp", {}) or {}).get("connection_file", "")
                  or (config.get("ColabKernelApp", {}) or {}).get("connection_file", ""))
    return "jupyter" in connection.lower()


def in_vscode_notebook():
    shell = sys.modules.get("IPython")
    instance = getattr(shell, "get_ipython", lambda: None)() if shell else None
    kernel = getattr(instance, "kernel", None)
    return bool(kernel and getattr(getattr(kernel, "shell", None), "user_ns", {}).get("__vsc_ipynb_file__") is not None)


class Settings:
    """Validated public W&B settings plus explicit Backprop transport controls."""

    def __init__(self, **kwargs):
        object.__setattr__(self, "_values", copy.deepcopy(_DEFAULTS))
        object.__setattr__(self, "_explicit", set())
        self._values.update(anonymous=_ANONYMOUS_UNSET, root_dir=os.getcwd(), credentials_file=default_path(),
                            symlink=platform.system() != "Windows", flush_interval=5.0, console="wrap")
        for key, value in kwargs.items():
            setattr(self, key, value)
        self._validate_branching()

    def __getattr__(self, name):
        try:
            return self._values[name]
        except KeyError as error:
            raise AttributeError(name) from error

    def __setattr__(self, name, value):
        if name.startswith("_"):
            object.__setattr__(self, name, value)
            return
        if name not in self._values:
            raise UsageError(f"Unknown Settings field: {name}")
        value = self._validate(name, value)
        from .settings_policy import validate_requested_feature
        validate_requested_feature(name, value)
        self._values[name] = value
        self._explicit.add(name)

    @staticmethod
    def _validate(name, value):
        """Coerce documented field types, then validate field-specific limits.

        Main callers: constructor, setting assignment and source-precedence merge.
        """
        annotation = _TYPES.get(name, "float")
        if name == "x_stats_sampling_interval" and (not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or value < 1):
            raise UsageError("x_stats_sampling_interval must be at least one second")
        if name == "anonymous":
            if value is not _ANONYMOUS_UNSET:
                import backprop
                backprop.termwarn("The anonymous setting has no effect and will be removed"
                                  " in a future version.", repeat=False)
            return value
        if value is None:
            if "None" in annotation or name == "anonymous":
                return None
            raise UsageError(f"Settings.{name} cannot be None")
        if name == "resume" and isinstance(value, bool):
            return "auto" if value else None
        if "bool" in annotation and "Literal" not in annotation:
            if isinstance(value, str) and value.lower() in {"true", "false", "1", "0", "yes", "no"}:
                value = value.lower() in {"true", "1", "yes"}
            if not isinstance(value, bool):
                raise UsageError(f"Settings.{name} must be a boolean")
        elif annotation.startswith("float") or name == "flush_interval":
            try:
                value = float(value)
            except (TypeError, ValueError) as error:
                raise UsageError(f"Settings.{name} must be numeric") from error
        elif annotation.startswith("int"):
            if isinstance(value, bool):
                raise UsageError(f"Settings.{name} must be an integer")
            try:
                number = int(value)
                if float(value) != number:
                    raise ValueError()
                value = number
            except (TypeError, ValueError) as error:
                raise UsageError(f"Settings.{name} must be an integer") from error
        elif name in {"config_paths", "ignore_globs", "run_tags"}:
            if isinstance(value, str):
                value = [value]
            if not isinstance(value, (tuple, list)) or not all(isinstance(x, str) for x in value):
                raise UsageError(f"Settings.{name} must be a sequence of strings")
            value = tuple(value)
        elif annotation.startswith("str"):
            if isinstance(value, os.PathLike):
                value = os.fspath(value)
            if not isinstance(value, str):
                raise UsageError(f"Settings.{name} must be a string")
        elif annotation.startswith("dict"):
            if isinstance(value, str):
                value = json.loads(value)
            if not isinstance(value, dict):
                raise UsageError(f"Settings.{name} must be a dictionary")
            if not all(isinstance(k, str) and isinstance(v, str) for k, v in value.items()):
                raise UsageError(f"Settings.{name} must map strings to strings")
            if name == "capture_loggers":
                import logging
                for level in value.values():
                    try:
                        logging._checkLevel(level)
                    except (TypeError, ValueError) as error:
                        raise UsageError(f"Invalid logger level: {level!r}") from error
        elif name in {"fork_from", "resume_from"}:
            if isinstance(value, str):
                value = RunMoment.from_uri(value)
            elif isinstance(value, dict):
                value = RunMoment(**value)
            if not isinstance(value, RunMoment):
                raise UsageError(f"Settings.{name} must be a RunMoment or run moment URI")
        elif name == "stop_fn" and not callable(value):
            raise UsageError("Settings.stop_fn must be callable")
        enums = {"mode": {"online", "offline", "disabled", "shared", "run", "dryrun"},
                 "console": {"auto", "off", "wrap", "redirect", "wrap_raw", "wrap_emu"},
                 "resume": {"allow", "must", "never", "auto"},
                 "reinit": {"default", "return_previous", "finish_previous", "create_new", True, False},
                 "job_source": {"repo", "artifact", "image"}}
        if name in enums and not any(value == option for option in enums[name]):
            raise UsageError(f"Invalid Settings.{name}: {value!r}")
        if name in {"init_timeout", "heartbeat_seconds", "flush_interval"} and value <= 0:
            raise UsageError(f"Settings.{name} must be positive")
        if name in {"finish_timeout", "login_timeout", "console_chunk_max_bytes", "console_chunk_max_seconds",
                    "max_end_of_run_history_metrics", "max_end_of_run_summary_metrics", "summary_warnings"} and value < 0:
            raise UsageError(f"Settings.{name} must not be negative")
        if name == "run_id" and (not value or value in {".", ".."} or any(c in value for c in "/\\#?%:")):
            raise UsageError("Run ID must be nonempty and cannot contain / \\ # ? % :")
        if name == "project" and (not value or len(value) > 128 or any(c in value for c in "/\\#?%:")):
            raise UsageError("Invalid project name")
        if name == "run_tags" and any(not tag or len(tag) > 64 for tag in value):
            raise UsageError("Tags must be nonempty strings of at most 64 characters")
        if name in {"base_url", "http_proxy", "https_proxy"} and urlsplit(value).scheme not in {"http", "https"}:
            raise UsageError(f"Settings.{name} requires an HTTP(S) URL")
        if name == "base_url":
            value = value.rstrip("/")
        if name == "api_key" and value != value.strip():
            raise UsageError("API key cannot contain surrounding whitespace")
        if isinstance(value, float) and not math.isfinite(value):
            raise UsageError(f"Settings.{name} must be finite")
        if name in {"code_dir", "root_dir", "credentials_file", "settings_system", "git_root", "program_abspath"}:
            value = str(Path(value).expanduser())
        if name == "console" and value == "auto":
            value = "wrap"
        return value

    def _validate_branching(self):
        if sum(value is not None for value in (self.resume, self.resume_from, self.fork_from)) > 1:
            raise UsageError("resume, resume_from and fork_from are mutually exclusive")

    def model_copy(self, *, update=None, deep=False):
        result = Settings()
        object.__setattr__(result, "_values", {key: copy.deepcopy(value) if deep and not callable(value) else value for key, value in self._values.items()})
        object.__setattr__(result, "_explicit", self._explicit.copy())
        for name, value in self.__dict__.items():
            if name not in {"_values", "_explicit"}:
                object.__setattr__(result, name, copy.deepcopy(value) if deep else value)
        if update:
            for key, value in update.items():
                if key == "anonymous":
                    # Pinned model_copy performs no field validation. Preserve
                    # that deprecated-field behavior without weakening the
                    # validation required for effective runtime settings.
                    result._values[key] = value
                    result._explicit.add(key)
                else:
                    setattr(result, key, value)
            result._validate_branching()
        return result

    def model_dump(self, *, exclude=None, include=None, exclude_none=False, **kwargs):
        result = {key: copy.deepcopy(value) if not callable(value) else value
                  for key, value in self._values.items() if key != "anonymous"}
        return {key: value for key, value in result.items() if key not in (exclude or ())
                and (include is None or key in include) and (not exclude_none or value is not None)}

    def model_dump_json(self, **kwargs):
        return json.dumps(self.model_dump(**kwargs), default=str)

    @property
    def model_fields_set(self):
        return self._explicit.copy()

    def update_from_dict(self, settings):
        """Validate a complete update before applying any fields."""
        candidate = self.model_copy()
        for name, value in settings.items():
            if value is not None:
                setattr(candidate, name, value)
        candidate._validate_branching()
        object.__setattr__(self, "_values", candidate._values)
        self._explicit.update(candidate._explicit)

    def update_from_settings(self, settings):
        self.update_from_dict({key: settings._values[key] for key in settings._explicit})

    def read_system_settings(self):
        from .settings_files import SettingsFiles
        return SettingsFiles(global_settings=self.settings_system, local_settings=self.settings_workspace)

    @staticmethod
    def _read_file(path):
        parser = configparser.ConfigParser()
        if not Path(path).is_file():
            return {}
        parser.read(path)
        return dict(parser["default"]) if parser.has_section("default") else {}

    def update_from_system_settings(self):
        values = self.read_system_settings().all()
        for forbidden in ("root_dir", "settings_system"):
            if forbidden in values:
                warnings.warn(f"Ignoring {forbidden!r}: it is not allowed in settings files", RuntimeWarning)
                del values[forbidden]
        if "ignore_globs" in values:
            values["ignore_globs"] = values["ignore_globs"].split(",")
        if "anonymous" in values:
            import backprop
            backprop.termwarn("Deprecated setting 'anonymous' has no effect and will be"
                              " removed in a future version of wandb."
                              " Please delete it manually or by running `wandb login`"
                              " to avoid errors.", repeat=False)
            values["anonymous"] = _ANONYMOUS_UNSET
        try:
            self.update_from_dict(values)
        except UnsupportedError:
            raise
        except (TypeError, ValueError, UsageError) as error:
            warnings.warn(f"Settings file is invalid; no values applied: {error}", RuntimeWarning)

    def update_from_env_vars(self, environ):
        aliases = {"DIR": "root_dir", "NAME": "run_name", "NOTES": "run_notes", "TAGS": "run_tags",
                   "RUN_GROUP": "run_group", "JOB_TYPE": "run_job_type", "APP_URL": "app_url_override", "USER_EMAIL": "email"}
        values = {}
        for key, value in environ.items():
            if key == "WANDB_API_KEY" and value:
                if value != "backprop-foundry-readiness-sentinel":
                    warnings.warn("Ignoring WANDB_API_KEY: Backprop uses credentials for the backend selected by BACKPROP_CONFIG", RuntimeWarning, stacklevel=2)
                continue
            if value is not None and key.startswith("WANDB_"):
                field = aliases.get(key[6:], key[6:].lower())
                if field in self._values:
                    values[field] = value.split(",") if field in {"run_tags", "ignore_globs", "config_paths"} else value
        self.update_from_dict(values)

    def update_from_system_environment(self):
        from .settings_environment import refresh_environment
        refresh_environment(self)

    @property
    def _offline(self):
        return self.mode in {"offline", "dryrun"}

    @property
    def wandb_dir(self):
        from .settings_paths import workspace_dir
        return getattr(self, "_wandb_dir", None) or str(workspace_dir(self.root_dir, self.use_dot_wandb))

    @property
    def resume_fname(self):
        return getattr(self, "_resume_fname", None) or str(Path(self.wandb_dir) / "wandb-resume.json")

    @property
    def run_mode(self):
        return "offline-run" if self._offline else "run"

    @property
    def timespec(self):
        return time.strftime("%Y%m%d_%H%M%S", time.localtime(getattr(self, "_start_time", time.time())))

    @property
    def sync_dir(self):
        return getattr(self, "_sync_dir", None) or str(Path(self.wandb_dir) / f"{self.run_mode}-{self.timespec}-{self.run_id}")

    @property
    def files_dir(self):
        return str(Path(self.sync_dir) / "files")

    @property
    def sync_file(self):
        return str(Path(self.sync_dir) / "events.jsonl")

    @property
    def settings_workspace(self):
        return str(Path(self.wandb_dir) / "settings")

    @property
    def log_dir(self):
        return str(Path(self.sync_dir) / "logs")

    @property
    def log_user(self):
        return str(Path(self.log_dir) / "debug.log")

    @property
    def log_internal(self):
        return str(Path(self.log_dir) / "debug-internal.log")

    @property
    def sync_symlink_latest(self):
        return str(Path(self.wandb_dir) / "latest-run")

    @property
    def log_symlink_user(self):
        return str(Path(self.wandb_dir) / "debug.log")

    @property
    def log_symlink_internal(self):
        return str(Path(self.wandb_dir) / "debug-internal.log")

    @property
    def app_url(self):
        return self.app_url_override or getattr(self, "_app_url", None)

    @property
    def run_url(self):
        if not self.app_url or self._offline or self.mode == "disabled":
            return None
        return f"{self.project_url}&run={quote(self.run_id or '')}"

    @property
    def project_url(self):
        if not self.app_url or self._offline or self.mode == "disabled":
            return None
        return f"{self.app_url.rstrip('/')}?project={quote((self.entity or '')+'/'+(self.project or ''), safe='')}"

    # Sweep URL access is installed as an explicit unsupported guard.
    @property
    def colab_url(self):
        from .notebook_providers import colab_url
        return colab_url(self)
    deployment = property(lambda self: "local" if self.is_local else "cloud")
    @property
    def is_local(self):
        """Use the pinned SDK's custom-server label, not a physical location.

        Main callers: deployment and compatibility clients inspecting settings.
        W&B names every URL other than its exact default endpoint "local";
        Foundry therefore has that label even when its infrastructure is hosted.
        """
        return str(self.base_url) != "https://api.wandb.ai"

    def infer_git_root(self):
        """Set git_root from root_dir when no explicit root or disable flag exists."""
        if self.disable_git or self.git_root is not None:
            return
        root = Path(self.root_dir).resolve()
        for candidate in (root, *root.parents):
            if (candidate / ".git").exists():
                self.git_root = str(candidate)
                return
