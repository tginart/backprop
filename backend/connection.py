"""
Purpose: Select a backend from one connection configuration for every SDK caller.
Input: A connection file, documented environment overrides, and SDK settings.
Output: A store or authentication result from the selected backend adapter.
Note: Legacy configurations without a selector continue to select Foundry.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable

from .auth import AuthCandidate


def load_config(path: str | Path | None = None) -> dict:
    """
    Purpose: Read one connection file and apply the existing environment overrides.
    Input: An optional connection file path.
    Output: A configuration for one supported backend.
    """
    path = path or os.environ.get("BACKPROP_CONFIG")
    data = json.loads(Path(path).read_text()) if path else {}
    for field, environment in {
        "backend": "BACKPROP_BACKEND", "host": "BACKPROP_HOST",
        "stream_dataset_rid": "BACKPROP_STREAM_DATASET_RID",
        "media_dataset_rid": "BACKPROP_MEDIA_DATASET_RID", "app_url": "BACKPROP_APP_URL",
    }.items():
        if os.environ.get(environment):
            data[field] = os.environ[environment]
    return normalize_config(data)


def normalize_config(config: dict) -> dict:
    if _backend_name(config) == "gcp":
        from .gcp.config import normalize_gcp_config
        return normalize_gcp_config(config)
    if _backend_name(config) == "local":
        from .local.config import normalize_local_config
        return normalize_local_config(config)
    return dict(config)


def create_store(config: dict, token: str | None = None) -> Any:
    """
    Purpose: Build the selected adapter without an SDK backend branch.
    Input: A connection dictionary and an optional explicit credential.
    Output: A store with the shared event and artifact methods.
    """
    if _backend_name(config) == "gcp":
        from .gcp.store import GcpStore
        return GcpStore(config, token=token)
    if _backend_name(config) == "local":
        from .local.store import LocalStore
        return LocalStore(config)
    from .foundry.store import FoundryStore
    if not config.get("host") or not config.get("stream_dataset_rid"):
        from backprop.transport import BackpropError
        raise BackpropError("Online logging requires BACKPROP_CONFIG with host and stream_dataset_rid; use mode='offline' for local logging")
    return FoundryStore(config, token=token)


def authenticate_candidate(*, load_config: Callable, native_store: Callable,
                           config: dict | None = None, **options: Any) -> Any:
    """
    Purpose: Apply the selected backend's credential policy before SDK publication.
    Input: The SDK authentication options and configuration/store factories.
    Output: An isolated credential result, an offline choice, or no selection.
    Note: Legacy availability checks do not require a connection file.
    """
    if config is None and (os.environ.get("BACKPROP_CONFIG") or os.environ.get("BACKPROP_BACKEND")):
        config = load_config()
    if _backend_name(config or {}) == "gcp":
        from .gcp.auth import authenticate_candidate as authenticate
        return authenticate(config=config, native_store=native_store, **options)
    if _backend_name(config or {}) == "local":
        from .local.auth import authenticate_candidate as authenticate
        return authenticate(config=config, native_store=native_store, **options)
    from .foundry.login_session import authenticate_candidate as authenticate
    return authenticate(load_config=load_config, native_store=native_store, **options)


def persist_candidate(candidate: AuthCandidate, settings: Any) -> None:
    if candidate.backend == "foundry":
        from .foundry.login_session import persist_candidate as persist
        persist(candidate, settings)


def configure_settings(settings: Any, candidate: AuthCandidate) -> None:
    if candidate.backend == "foundry":
        settings.api_key = candidate.token
        if candidate.host is not None:
            settings.base_url = candidate.host


def publish_candidate(candidate: AuthCandidate) -> None:
    if candidate.backend == "gcp" and candidate.token is not None:
        os.environ["BACKPROP_GCP_ACCESS_TOKEN"] = candidate.token
    if candidate.backend == "foundry":
        os.environ["BACKPROP_TOKEN"] = candidate.token
        if candidate.host is not None:
            os.environ["BACKPROP_HOST"] = candidate.host


def credentials_path() -> str:
    from .foundry.credential_store import default_path
    return default_path()


def credential_values() -> list[str | None]:
    return [os.environ.get(name) for name in
            ("FOUNDRY_TOKEN", "BACKPROP_TOKEN", "WANDB_API_KEY", "BACKPROP_GCP_ACCESS_TOKEN")]


def snapshot_store(store: Any) -> dict:
    from .gcp.store import GcpStore
    from .http_store import HttpStore
    if isinstance(store, GcpStore):
        return {"kind": "backend", "config": dict(store.config),
                "token": store._explicit_token}
    if isinstance(store, HttpStore):
        return {"kind": "backend", "config": dict(store.config), "token": store.token}
    return {"kind": "provided", "sink": store}


def restore_store(descriptor: dict) -> Any:
    if descriptor["kind"] in {"backend", "foundry"}:
        return create_store(descriptor["config"], token=descriptor["token"])
    if descriptor["kind"] == "provided":
        return descriptor["sink"]
    raise ValueError("Unknown Backprop logger transport")


def _backend_name(config: dict) -> str:
    name = config.get("backend", "foundry")
    if name not in {"foundry", "local", "gcp"}:
        raise ValueError("Unknown Backprop backend")
    return name


def prepare_init_candidate(settings: Any, supplied: Any, deployment: dict | None,
                           *, load_config: Callable, native_store: Callable,
                           setup: Any = None) -> tuple[dict, AuthCandidate]:
    """
    Purpose: Select backend authentication before the SDK changes its active run.
    Input: The resolved settings, caller settings, and connection factories.
    Output: A detached connection and authentication result.
    """
    config = dict(deployment if deployment is not None else load_config())
    if _backend_name(config) == "gcp":
        explicit = supplied if isinstance(supplied, dict) else getattr(supplied, "_explicit", set())
        key = settings.api_key if "api_key" in explicit else None
        candidate = authenticate_candidate(config=config, load_config=lambda: config,
            native_store=native_store, key=key, host=config.get("host"))
        return config, candidate
    if _backend_name(config) == "local":
        candidate = authenticate_candidate(config=config, load_config=lambda: config,
            native_store=native_store, host=config.get("host"))
        return config, candidate
    from .foundry.login_session import prepare_init_candidate as prepare
    return prepare(settings, supplied, config, load_config=load_config,
                   native_store=native_store, setup=setup)


def current_init_session(host: str | None = None) -> AuthCandidate | None:
    from .foundry.login_session import current_init_session as current
    if host is not None and host.startswith("http:"):
        return None
    return current(host)


def remember_init_session(candidate: AuthCandidate) -> None:
    if candidate.backend == "foundry":
        from .foundry.login_session import remember_init_session as remember
        remember(candidate)


def clear_init_session() -> None:
    from .foundry.login_session import clear_init_session as clear
    clear()
