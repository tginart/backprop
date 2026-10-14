"""Public exception identities shared by imports, configuration and run logging.

Architecture: transport's existing BackpropError is the root Error alias;
validation/authentication specializations retain the familiar W&B hierarchy.
Main callers: public module exports and all core validation boundaries.
"""
from .transport import BackpropError

Error = BackpropError


class UsageError(Error):
    """A public argument or logging lifecycle is invalid."""


class CommError(Error):
    """A public API failure retaining its original exception for inspection."""

    def __init__(self, msg, exc=None):
        self.exc = exc
        self.message = msg
        super().__init__(msg)


class AuthenticationError(CommError):
    """No valid Foundry credentials are available."""


class ConfigError(Error, ValueError):
    """A protected configuration value was changed."""


class UnsupportedError(UsageError):
    """A requested SDK feature is unknown or unavailable."""


class UnsupportedFeatureError(UnsupportedError, NotImplementedError):
    """A known Backprop feature is absent; catch by either public error family.

    Why this exists: the pinned UnsupportedError hierarchy remains unchanged for
    require() and SDK compatibility. Missing Backprop operations additionally
    support Python's conventional NotImplementedError handler.
    """
