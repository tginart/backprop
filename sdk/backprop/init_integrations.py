"""Resolve init-only defaults and hooks before replacing an existing run.

Architecture: selected init settings -> detached metadata defaults -> temporary
TensorBoard hook ownership -> real or reused run -> release the temporary owner.
Main caller: backprop.init. Why this exists: a missing or broken integration
must fail before reinit finishes a currently usable training run. Disabled
handles have distinct metadata defaults and skip hooks, like the pinned SDK.
"""
from contextlib import contextmanager
import os
from pathlib import Path
import subprocess
import tempfile
import uuid


def prepare_run_defaults(settings):
    """Infer absent run metadata without changing the caller's Settings.

    Main caller: init, before its active-run policy and Run construction.
    Why this exists: disabled handles use temporary-root dummy metadata, while
    normal runs derive an absent project from the actual working repository.
    Git discovery only reads local paths and respects disable_git. This stage
    resolves init-only defaults; direct Run construction remains separate.
    """
    selected = settings.model_copy()
    if selected.project is None:
        selected.project = _project_name(selected)
    if selected.mode == 'disabled':
        selected.run_id = selected.run_id or (selected.resume_from.run if selected.resume_from else uuid.uuid4().hex[:8])
        selected.run_name = selected.run_name or f'dummy-{selected.run_id}'
        selected.project = 'dummy' if selected.project == 'uncategorized' else selected.project
        selected.entity = selected.entity or 'dummy'
        selected.root_dir = tempfile.gettempdir()
    return selected


def _project_name(settings):
    """Apply the pinned working-directory and program project heuristic.

    Main caller: prepare_run_defaults for an absent project. A directory outside
    Git normally selects uncategorized; unsupported executable discovery is
    likewise absent, as in the pinned heuristic. Explicit Git privacy skips
    this inspection rather than reading repository identity indirectly.
    """
    if settings.disable_git:
        return 'uncategorized'
    environment = {name: value for name, value in os.environ.items()
                   if name not in {'GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE'}}
    try:
        process = subprocess.run([os.environ.get('GIT_PYTHON_GIT_EXECUTABLE') or 'git',
                                  'rev-parse', '--show-toplevel'],
                                 capture_output=True, text=True, timeout=2,
                                 env=environment, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return 'uncategorized'
    if process.returncode != 0:
        return 'uncategorized'
    root = process.stdout.strip()
    from .settings_environment import launch_program
    program = settings.program or launch_program(settings)
    name = Path(root).name
    if program is not None:
        program_directory = os.path.dirname(os.path.abspath(program))
        if program_directory.startswith(root):
            relative = os.path.relpath(program_directory, root)
            if relative != '.':
                name += '-' + relative
    return name.replace(os.sep, '_')


class _HookLease:
    """Weak-referenceable owner used only while init chooses its run."""


@contextmanager
def prepared_integrations(settings, monitor_gym):
    """Validate/install hooks, then relinquish only this init's ownership."""
    if settings.mode == "disabled":
        yield None
        return
    if monitor_gym:
        from .integration.gym import monitor
        monitor()
    lease = None
    if settings.sync_tensorboard:
        from .integration import tensorboard
        lease = _HookLease()
        tensorboard.register_run(lease)
    try:
        yield lease
    finally:
        if lease is not None:
            tensorboard.release_run(lease)


def adopt_tensorboard(run):
    """Transfer automatic hook use to a newly created or reused live handle.

    Main callers: Run construction and init(return_previous). Registration is
    idempotent. Public settings retain the original call's options even when a
    later init enables interception for the existing run.
    """
    if run.disabled or run._tensorboard_registered:
        return
    from .integration import tensorboard
    tensorboard.register_run(run)
    run._tensorboard_registered = True
    run.define_metric("global_step")
    run.define_metric("*", step_metric="global_step", step_sync=True)
