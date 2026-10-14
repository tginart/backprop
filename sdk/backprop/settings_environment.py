"""Portable environment refresh and launch-program discovery.

Architecture: refresh explicit environment preferences -> discover the notebook
when applicable -> fill absent host/container/program -> infer existing local
code paths. Refreshing never overrides a caller's save_code=False. Vendor
container metadata services and private executable telemetry are separate.
Main callers: Settings.update_from_system_environment and Run settings setup.
"""
from __future__ import annotations

import os
from pathlib import Path
import socket
import sys


def _truth(value):
    return isinstance(value, str) and value.lower() in {"y", "yes", "t", "true", "on", "1"}


def launch_program(settings):
    """Use the notebook, explicit environment, or Python's actual entry module.

    Main callers: refresh_environment. sys.argv[0] cannot distinguish a module
    launch from a file, and pytest/embedded processes can rewrite it independently.
    """
    from .settings import in_jupyter, in_vscode_notebook
    if in_jupyter() or in_vscode_notebook():
        return settings.notebook_name or getattr(settings, "_notebook_path", None) or settings.program
    if "WANDB_PROGRAM" in os.environ:
        return os.environ["WANDB_PROGRAM"]
    main = sys.modules.get("__main__")
    if main is None:
        return None
    spec = getattr(main, "__spec__", None)
    return f"-m {spec.name}" if spec is not None else getattr(main, "__file__", None)


def code_paths(settings, program):
    """Infer paths only for actual files, preserving a caller's relative path.

    Main callers: refresh_environment. A module-launch description stays a
    description rather than being mistaken for a file literally named '-m ...'.
    """
    notebook_root = getattr(settings, "_notebook_root", None)
    if notebook_root:
        absolute = Path(notebook_root, program).absolute()
        settings.program_abspath = str(absolute)
        settings.program_relpath = program
        return
    candidate = Path(program).absolute()
    if not candidate.exists():
        return
    root = Path.cwd()
    if not settings.disable_git:
        from .run_metadata import _git
        found = _git(root, "rev-parse", "--show-toplevel")
        if found:
            root = Path(found)
    if settings.program_relpath is None:
        try:
            settings.program_relpath = candidate.relative_to(root).as_posix()
        except ValueError:
            pass
    settings.program_abspath = str(candidate)


def refresh_environment(settings, *, discover=True):
    """Apply pinned portable refresh precedence without reading secret env data."""
    if settings.save_code in {True, None} and any(name in os.environ for name in ("WANDB_SAVE_CODE", "WANDB_DISABLE_CODE")):
        settings.save_code = _truth(os.environ.get("WANDB_SAVE_CODE")) and not _truth(os.environ.get("WANDB_DISABLE_CODE"))
    if "WANDB_DISABLE_GIT" in os.environ:
        # This intentionally differs from generic boolean Settings coercion:
        # pinned env.disable_git treats every string except 'false' as true.
        settings.disable_git = os.environ["WANDB_DISABLE_GIT"].lower() != "false"
    from .settings import in_jupyter, in_vscode_notebook
    if discover and (in_jupyter() or in_vscode_notebook()):
        from .notebook_discovery import resolve_notebook
        resolve_notebook(settings)
    if settings.host is None:
        settings.host = socket.gethostname()
    if settings.docker is None and "WANDB_DOCKER" in os.environ:
        settings.docker = os.environ["WANDB_DOCKER"]
    program = settings.program or launch_program(settings)
    if discover and program is not None and not settings.disable_code:
        code_paths(settings, program)
    settings.program = program if program is not None else ("<python with no main file>" if discover else None)
