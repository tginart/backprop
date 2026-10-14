"""Pinned W&B requirement parsing without an external feature service.

Architecture: select requirement or its experiment alias -> normalize each
feature's optional version/variant -> accept the three pinned built-ins -> warn
for every unknown feature and finally raise its last UnsupportedError. Core and
service are already the default runtime and therefore retain their no-op warning.
Main callers: backprop.require and the compatible wandb module alias.
"""
from .errors import UnsupportedError


def apply_requirements(requirement, experiment, warn):
    """Apply all requirements in order before reporting the last unknown one.

    Main callers: the public require wrapper. An empty primary argument falls
    through to experiment, matching pinned 0.30.0; no arguments are a no-op.
    """
    features = requirement or experiment
    if not features:
        return
    features = [features] if isinstance(features, str) else tuple(features)
    last_error = None
    for item in features:
        name = item.split("@", 2)[0].split(":", 2)[0].replace("-", "_")
        if name in {"core", "service"}:
            warn(f"`wandb.require('{name}')` is a no-op as it is now the default behavior.")
        elif name != "require":
            # Error text uses the original feature spelling, while dispatch
            # normalizes hyphens just as pinned require_<feature> lookup does.
            feature = item.split("@", 2)[0].split(":", 2)[0]
            last_error = f"require() unsupported requirement: {feature}"
            warn(last_error)
    if last_error:
        raise UnsupportedError(last_error)
