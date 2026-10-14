"""Select and verify Foundry credentials before publishing a new session.

Architecture: explicit token -> reusable environment/cache/settings credentials
-> optional terminal prompt -> isolated verification -> caller publication.
Relogin skips reusable credentials, but an explicitly passed key still wins.
Main callers: login, online Run construction and the public Api factory. Workers
perform verification only; a late timeout worker cannot publish credentials.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlsplit

from .errors import AuthenticationError, UsageError
from .runtime import bounded_call

_init_session = None


def clear_init_session():
    """Forget process credentials when public teardown resets SDK context.

    Main callers: teardown and setup replacing explicit session credentials.
    This clears only in-memory selection, never credential files or environment.
    """
    global _init_session
    _init_session = None


def remember_init_session(candidate):
    """Publish one successful selection for subsequent init calls.

    Main callers: init preparation and successful public login. Why this
    exists: a selected session precedes later environment/cache changes in the
    pinned SDK. Failed auth cannot replace it, and relogin can bypass it.
    """
    global _init_session
    if candidate.mode == 'online':
        _init_session = candidate


@dataclass(frozen=True, repr=False)
class AuthCandidate:
    """A resolved token or explicit local-mode choice; repr never prints tokens."""
    token: str | None
    host: str | None
    source: str = 'explicit'
    mode: str = 'online'


def current_init_session(host=None):
    """Read the immutable selected session, optionally restricted to one host.

    Main callers: public Api credential preparation. Why this exists: init and
    login already publish this session before later environment changes. Other
    public factories must reuse it without importing private mutable globals.
    The returned candidate has no token repr; consumers must forward its host
    together with its key to authenticate_candidate. A foreign requested host
    returns no candidate, preserving implicit credential isolation.
    """
    candidate = _init_session
    if candidate is None:
        return None
    if host is not None and (candidate.host is None or
            _host_origin(host).lower() != _host_origin(candidate.host).lower()):
        return None
    return candidate


def _host_origin(value):
    """Validate the same HTTPS-origin boundary used by FoundryStore."""
    if (not isinstance(value, str) or value != value.strip() or
            any(ord(char) <= 32 or ord(char) == 127 for char in value)):
        raise UsageError('Foundry host must be an HTTPS origin without a path or credentials')
    normalized = value.rstrip('/')
    try:
        parsed = urlsplit(normalized)
        parsed.port
    except ValueError as error:
        raise UsageError('Foundry host must be a valid HTTPS origin') from error
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.path or
            parsed.query or parsed.fragment or parsed.username is not None or
            parsed.password is not None):
        raise UsageError('Foundry host must be an HTTPS origin without a path or credentials')
    if parsed.hostname.endswith(('wandb.ai', 'wandb.com')):
        raise UsageError('Backprop login requires a Foundry origin; omit the host to use BACKPROP_CONFIG')
    return normalized


def _environment_value(value, description, error_type):
    """Reject values that cannot be published before either credential changes."""
    try:
        if '\0' in value: raise ValueError()
        os.fsencode(value)
    except (ValueError, UnicodeError):
        raise error_type(f'{description} cannot be represented in the process environment') from None


def validate_token(value):
    """Validate an opaque Foundry bearer token without imposing W&B key formats.

    Main callers: selection and credential-file reads/writes. Errors contain no
    submitted value, including when a terminal input or cache file is malformed.
    """
    if not isinstance(value, str) or not value or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value):
        raise AuthenticationError('Foundry token must be a nonempty string without whitespace or control characters')
    _environment_value(value, 'Foundry token', AuthenticationError)
    return value


def _publish_init_selection(candidate, setup):
    """Publish selected auth/defaults to the process-owned setup handle.

    Main caller: prepare_init_candidate after authentication selection succeeds.
    Why this exists: pinned init updates its singleton before backend startup;
    a denied publication therefore keeps selected credentials, while prompt
    offline/timeout choices affect later omitted-mode calls. Caller-provided
    Settings remain detached. Offline clears session auth, whereas a prompt
    timeout retains it while disabling logging, matching the pinned stages.
    """
    if setup is None:
        from .run import resolve_settings
        defaults = resolve_settings(None)
    else:
        defaults = setup.model_copy(deep=True)
    if candidate.mode != 'online':
        defaults.mode = candidate.mode
        if candidate.mode == 'offline':
            defaults.api_key = None
            if setup is not None:
                # Generic settings inheritance omits None values. This is an
                # explicit auth clear after a chosen offline prompt, so apply
                # it to the owned handle rather than inheriting the old key.
                setup.api_key = None
            clear_init_session()
    else:
        defaults.api_key = candidate.token
        if candidate.host is not None:
            defaults.base_url = candidate.host
    from . import _publish_login_defaults
    _publish_login_defaults(defaults)


def authenticate_candidate(*, key, host, prompt, verify, timeout, settings,
                           load_config, native_store, relogin=None, force=None, referrer=None,
                           session_key=None, session_host=None, raise_prompt_timeout=False):
    """Return a verified selection without changing process credentials or files.

    Main callers: public login and online factories. Prompt cancellation leaves
    the previous session untouched; an explicit offline choice returns its mode.
    Config/store callables preserve each caller's existing transport boundary.
    Init may supply a host-scoped setup/login session; relogin skips that
    reusable source, while an explicit per-call key still takes precedence.
    Inspection callers propagate prompt timeout because they cannot switch to
    disabled logging. Logging callers retain the existing local-mode selection.
    """
    from .credential_store import read_token
    from .login_prompt import prompt_token
    if key is not None: validate_token(key)
    relogin = settings.relogin if relogin is None else relogin
    force = settings.force if force is None else force
    timeout = settings.login_timeout if timeout is None else timeout
    settings_host = settings.base_url if 'base_url' in settings._explicit else None
    selected_host = host if host is not None else settings_host or os.environ.get('BACKPROP_HOST')
    candidate_host = _host_origin(selected_host) if selected_host is not None else None
    if candidate_host is not None: _environment_value(candidate_host, 'Foundry host', UsageError)
    config = None
    def configuration():
        nonlocal config
        if config is None: config = dict(load_config())
        return config
    def resolve_host():
        nonlocal candidate_host
        if candidate_host is None and configuration().get('host'):
            candidate_host = _host_origin(configuration()['host'])
            _environment_value(candidate_host, 'Foundry host', UsageError)
        return candidate_host
    token, source = key, 'explicit'
    if token is None and not relogin and session_key and session_host:
        if resolve_host() and _host_origin(session_host).lower() == candidate_host.lower():
            token, source = session_key, 'session'
    if token is None and not relogin:
        token = os.environ.get('BACKPROP_TOKEN') or os.environ.get('FOUNDRY_TOKEN')
        source = 'environment'
        if not token:
            # A missing cache is not a reason to load deployment configuration
            # in a noninteractive availability check.
            from pathlib import Path
            if candidate_host is not None or Path(settings.credentials_file).exists():
                if resolve_host(): token = read_token(settings.credentials_file, candidate_host)
            source = 'cache'
        if not token and settings.api_key:
            if host is None:
                token = settings.api_key
            else:
                owner = settings_host if settings_host is not None else configuration().get('host')
                if owner and _host_origin(owner).lower() == candidate_host.lower(): token = settings.api_key
            source = 'settings'
    prompted = not token
    if prompted and not prompt: return None
    if prompted: resolve_host()
    while True:
        if prompted:
            try:
                token = prompt_token(candidate_host, force=force, timeout=timeout, referrer=referrer)
            except TimeoutError:
                if raise_prompt_timeout:
                    raise
                from . import termwarn
                termwarn('Backprop disabled due to login prompt timeout')
                return AuthCandidate(None, candidate_host, 'prompt', 'disabled')
            if token is None: return AuthCandidate(None, candidate_host, 'prompt', 'offline')
            source = 'prompt'
        retry_prompt = prompted
        try:
            validate_token(token)
            if verify:
                retry_prompt = False
                selected_config = configuration()
                selected_config.update({name: getattr(settings, name) for name in
                                        ('http_proxy', 'https_proxy', 'insecure_disable_ssl')
                                        if name in settings._explicit})
                if resolve_host() is not None: selected_config['host'] = candidate_host
                store = native_store(selected_config, token=token)
                verifier = getattr(store, 'verify_credentials', None)
                if verifier is None:
                    raise AuthenticationError('Credential verification is unavailable for the configured store; use verify=False only for deferred verification')
                retry_prompt = prompted
                try:
                    bounded_call(verifier, timeout, 'login')
                except TimeoutError:
                    raise
                except Exception as error:
                    raise AuthenticationError('Foundry credentials could not be verified') from error
            elif source in {'explicit', 'prompt'}:
                resolve_host()
            return AuthCandidate(token, candidate_host, source)
        except AuthenticationError:
            if not retry_prompt: raise
            from . import termwarn
            termwarn('Foundry token was invalid or rejected; enter another token')


def prepare_init_candidate(settings, supplied, deployment, *, load_config, native_store, setup=None):
    """Select credentials before init can replace a currently usable handle.

    Main caller: public init, before config parsing, adapter setup and reinit.
    Why this exists: direct Run construction owns its own auth stage, whereas
    public init must authenticate even when it returns a previous Run. Only
    supplied api_key is explicit; inherited setup/login credentials are a
    reusable host-scoped session that relogin must skip. Selection performs no
    remote verification, matching pinned init's deferred backend verification.
    The returned candidate is consumed once by the eventual new Run.
    """
    config = dict(deployment if deployment is not None else load_config())
    if 'base_url' in settings._explicit:
        config['host'] = _host_origin(settings.base_url)
    if hasattr(supplied, '_explicit'):
        explicit_key = supplied.api_key if 'api_key' in supplied._explicit else None
    else:
        explicit_key = (supplied or {}).get('api_key')
    session_key = _init_session.token if _init_session is not None else (setup.api_key if setup is not None else None)
    session_host = _init_session.host if _init_session is not None else (
        (setup.base_url if 'base_url' in setup._explicit else config.get('host')) if setup is not None else None)
    auth_settings = settings.model_copy()
    if explicit_key is None and setup is not None and setup.api_key:
        # Setup/login credentials already have an owner origin above. Do not
        # relabel an inherited token with a caller's different base_url, or
        # make it reusable again after relogin explicitly skips the session.
        auth_settings.api_key = None
    try:
        candidate = authenticate_candidate(key=explicit_key, host=config.get('host'), prompt=True,
            verify=False, timeout=None, settings=auth_settings, load_config=lambda: config,
            native_store=native_store, session_key=session_key, session_host=session_host)
    except EOFError:
        # Pinned hidden/untimed terminal input reports cancellation as an
        # interrupt. Keep the private prompt's EOF contract distinct, and do
        # not publish settings, credentials or an active-run policy on abort.
        raise KeyboardInterrupt from None
    if candidate.mode != 'online':
        settings.mode = candidate.mode
    elif candidate.host is not None:
        config['host'] = candidate.host
    _publish_init_selection(candidate, setup)
    remember_init_session(candidate)
    return config, candidate


def persist_candidate(candidate, settings):
    """Save explicit/prompted credentials only after successful selection.

    Main callers: login; init/Api call this only for prompted tokens. A deferred
    explicit token with no configured host remains process-only with a warning;
    no credential is ever cached under an unspecified or W&B host.
    """
    if candidate.mode != 'online' or candidate.source not in {'explicit', 'prompt'}: return
    if candidate.host is None:
        from . import termwarn
        termwarn('No Foundry host configured; this token is available only in the current process')
        return
    from .credential_store import write_token
    write_token(settings.credentials_file, candidate.host, candidate.token)
