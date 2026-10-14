"""Explain unsupported runtime settings before any run data is persisted.

Architecture: Settings validates field shapes -> this boundary checks requested
effects -> Run performs supported logging behavior. Service-only requests fail;
optional presentation differences warn. Keeping this separate prevents accepting
a valid field name from being mistaken for implementing its runtime behavior.
Main callers: Run initialization and terminal setup.
"""
from .errors import UsageError, UnsupportedFeatureError
from .settings import in_notebook


_SERVICE_OPTIONS = {
    "identity_token_file": "W&B identity federation is unavailable; use a Foundry token",
    "organization": "W&B organization selection does not select Foundry permissions",
    "job_name": "W&B Launch job creation is outside the logging contract",
    "job_source": "W&B Launch job creation is outside the logging contract",
    "launch": "W&B Launch scheduling is outside the logging contract",
    "launch_config_path": "W&B Launch configuration is outside the logging contract",
    "sweep_id": "W&B sweep service integration is outside the logging contract",
    "sweep_param_path": "W&B sweep parameter loading is outside the logging contract",
}


def validate_requested_feature(field, value):
    """Reject explicitly enabled service effects before changing Settings.

    Main caller: Settings assignment/constructor. Disabling an unavailable
    service is valid; explicit activation never becomes a warning or no-op.
    """
    if field in _SERVICE_OPTIONS and value:
        raise UnsupportedFeatureError(f"Settings.{field} is unsupported: {_SERVICE_OPTIONS[field]}")
    if field in {'disable_job_creation', 'sagemaker_disable'} and value is False:
        raise UnsupportedFeatureError(f"Settings.{field}=False requests a service integration not implemented in Backprop")


def presentation_diagnostics(settings):
    """Reserved for portable presentation differences requiring diagnostics."""
    return []


def validate_logging_settings(settings):
    """Reject requested service effects and return portable-runtime warnings.

    Main callers: Run.__init__, before constructing the journal. Explicit false
    flags that disable absent integrations already achieve their requested effect.
    Credentials are never included in diagnostics.
    """
    if settings.sync_tensorboard and settings.mode != "disabled":
        import importlib.util
        if importlib.util.find_spec("tensorboard") is None:
            raise UsageError("sync_tensorboard requires the optional tensorboard package")
    for field, reason in _SERVICE_OPTIONS.items():
        if field in settings._explicit and getattr(settings, field):
            raise UnsupportedFeatureError(f"Settings.{field} is unsupported: {reason}")
    # force removes interactive offline choices. Credential selection runs
    # before public init reinitialization; direct Run callers select later.
    if not settings.disable_job_creation:
        raise UnsupportedFeatureError("Settings.disable_job_creation=False requests unsupported W&B Launch job creation")
    messages = presentation_diagnostics(settings)
    if "summary_timeout" in settings._explicit and settings.summary_timeout != 60:
        messages.append("Settings.summary_timeout has no remote wait to bound: the current process owns its summary; finish_timeout bounds uploads")
    if settings.save_code and not settings.disable_code and getattr(settings, "_notebook_discovery_error", None):
        messages.append(settings._notebook_discovery_error)
    return messages
