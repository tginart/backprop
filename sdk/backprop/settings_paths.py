"""Select the public W&B workspace directory without creating it.

Architecture: root_dir + explicit/automatic dot-directory preference -> workspace
root. Run places its native project/run journals below this root; Settings uses
the same selection for workspace configuration and latest-run debug links.
Main callers: Settings.wandb_dir and Run initialization.
"""
from pathlib import Path


def workspace_dir(root, use_dot_wandb=None):
    """Honor a forced directory choice or discover an existing .wandb entry.

    Why this exists: truth-testing None loses the SDK's existing-directory
    discovery behavior and can make settings reads disagree with actual logs.
    This routine is read-only; failures creating a selected path remain visible
    at the write boundary.
    """
    root = Path(root).expanduser()
    use_dot = (root / ".wandb").exists() if use_dot_wandb is None else use_dot_wandb
    return root / (".wandb" if use_dot else "wandb")
