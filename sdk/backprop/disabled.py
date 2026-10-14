"""Chainable inert values used by the pinned disabled logging contract.

Architecture: a disabled artifact call returns Noop -> arbitrary attributes and
supported calls keep returning that same inert object. Explicitly unsupported
artifact operations still raise; disabled mode does not pretend to implement them.
Nothing reaches persistence or a
network transport. Main callers: Run.log_artifact in disabled mode.
"""


class Noop:
    def __getattr__(self, name):
        from .unsupported import reject
        from .unsupported_catalog import BOUNDARIES
        api = 'wandb.Artifact.' + name
        if api in BOUNDARIES:
            reject(api)
        return self

    def __setattr__(self, name, value):
        from .unsupported import reject
        from .unsupported_catalog import BOUNDARIES
        api = 'wandb.Artifact.' + name
        if api in BOUNDARIES:
            reject(api)
        object.__setattr__(self, name, value)

    def __call__(self, *args, **kwargs):
        return self
