"""Portable config parsing exposed through wandb.helper and wandb.sdk.

Architecture: optional YAML file -> dict/Namespace/framework flags -> include
or exclude filter. Main callers: public helper APIs and config.parse_config,
which separately supplies Backprop's optional-None and Path conveniences.
The conversion follows W&B 0.30.0 wandb_helper.py and config_util.py (MIT;
see THIRD_PARTY_NOTICES.md). No parser input imports a framework or executes code.
"""
import inspect
import types
from pathlib import Path
from .errors import UsageError,ConfigError


def _read_config_file(filename):
    """Read the versioned value/description YAML used by W&B config files.

    Main callers: public parse_config and native optional config normalization.
    An empty file returns None after warning; the public dict converter then
    raises the pinned TypeError, while the native optional boundary can use {}.
    """
    import yaml
    path=Path(filename)
    if not path.exists():
        raise ConfigError(f"config file {filename} doesn't exist")
    try:
        stream=path.open()
    except OSError:
        raise ConfigError(f"Couldn't read config file: {filename}") from None
    with stream:
        try:loaded=yaml.safe_load(stream)
        except yaml.parser.ParserError:
            raise ConfigError("Invalid YAML in config yaml") from None
    if loaded is None:
        import backprop
        backprop.termwarn("Found an empty default config file (config-defaults.yaml). Proceeding with no defaults.")
        return None
    version=loaded.pop('wandb_version',None)
    if version is not None and version!=1:
        raise ConfigError('Unknown config version')
    return {key:value['value'] for key,value in loaded.items()}


def _to_dict(params):
    """Convert only the container forms recognized by the pinned helper.

    Main callers: parse_config. Attribute-shape guessing would misinterpret
    ordinary Namespace objects containing FLAGS or flag_values_dict members;
    framework branches require their actual module identity.
    """
    if isinstance(params,dict):return params
    module=inspect.getmodule(params)
    if module:
        tf_flags=isinstance(params,types.ModuleType) and module.__name__=='tensorflow.python.platform.flags'
        if tf_flags or module.__name__=='absl.flags':
            params=params.FLAGS;module=inspect.getmodule(params)
    if module and module.__name__=='absl.flags._flagvalues':
        return {name:params[name].value for name in dir(params)}
    if not hasattr(params,'__dict__'):
        raise TypeError('config must be a dict or have a __dict__ attribute.')
    if '__flags' in vars(params):
        if '__parsed' in vars(params):params._parse_flags()
        return vars(params)['__flags']
    return vars(params)


def parse_config(params,exclude=None,include=None):
    """Apply the full portable W&B helper signature without mutating inputs.

    Main callers: public wandb.helper.parse_config and native config parsing.
    With no filter a dict/Namespace returns its original dictionary, as upstream
    does; filters produce a fresh dictionary and preserve the contained values.
    """
    if exclude and include:
        raise UsageError('Expected at most only one of exclude or include')
    if isinstance(params,str):params=_read_config_file(params)
    params=_to_dict(params)
    if include:params={key:value for key,value in params.items() if key in include}
    if exclude:params={key:value for key,value in params.items() if key not in exclude}
    return params
