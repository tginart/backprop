"""Check real disabled init handles against the artifact publication registry.

Architecture: isolated official/native SDK -> optional offline active run ->
disabled global handle -> real Artifact.save -> observed publication routing.
Main caller: test_artifact_save. Only files and local W&B core are used; no
credentials, hosted services or transport mocks participate in the decision.
"""
from contextlib import ExitStack
import json
import sys
import tempfile
from unittest.mock import patch

if sys.argv[1] == 'native':
    from backprop.compatibility import install_wandb_aliases
    install_wandb_aliases()
import wandb


def observe(has_active):
    """Return which real run receives the draft while a disabled run is global."""
    with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
        settings = dict(mode='offline', console='off', silent=True, disable_git=True)
        active = None
        if has_active:
            active = wandb.init(project='active', dir=directory, settings=wandb.Settings(**settings))
            stack.callback(active.finish)
        disabled = wandb.init(project='disabled', mode='disabled')
        stack.callback(disabled.finish)
        created = []
        original_init = wandb.init

        def initialize(**kwargs):
            run = original_init(**kwargs, dir=directory)
            created.append(run)
            return run

        disabled_log = stack.enter_context(patch.object(disabled, 'log_artifact', wraps=disabled.log_artifact))
        active_log = stack.enter_context(patch.object(active, 'log_artifact', wraps=active.log_artifact)) if active else None
        stack.enter_context(patch.object(wandb, 'init', side_effect=initialize))
        artifact = wandb.Artifact('test-disabled-publication', 'test')
        artifact.save(project='automatic', settings=wandb.Settings(**settings))
        return {
            'automatic_runs': len(created),
            'automatic_projects': [run.project for run in created],
            'automatic_job_types': [run.job_type for run in created],
            'disabled_publications': disabled_log.call_count,
            'active_publications': active_log.call_count if active_log else 0,
        }


if __name__ == '__main__':
    print(json.dumps({str(active): observe(active) for active in (False, True)}))
