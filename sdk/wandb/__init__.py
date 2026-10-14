"""Optional source-tree W&B import alias with one shared Backprop runtime.

Main callers: unchanged training code with this repository's sdk on PYTHONPATH.
The installed distribution exposes backprop; its scoped launcher installs these
same aliases without replacing an independently installed wandb package on disk.
"""
from backprop.compatibility import install_wandb_aliases

install_wandb_aliases()
