"""Load one operator's local Foundry installation settings.

Architecture: explicit path or environment selection -> strict local JSON ->
resolved state paths -> installation binding checks before credentials/network.
The tracked example has no personal defaults. Generated state stays local, and
browser packaging copies a separate public allowlist rather than this config.
Main callers: deployment, provisioning and live verification commands.
Why this exists: an operator may build or manage another enrollment without
editing source, inheriting a developer's IDs or requiring Google Cloud.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = ROOT / "deploy" / "config.local.json"


def required_value(value, name):
    """Reject unset/template strings before they can select real resources.

    Main callers: config loading, state validation and public ZIP packaging.
    """
    if (not isinstance(value, str) or not value.strip() or value != value.strip()
            or re.search(r"[<>]|\$\{|\bYOUR[_-]|\bREPLACE[_-]?ME\b", value, re.I)):
        raise ValueError(f"{name} must be a completed, nonempty value, not a template placeholder")
    return value


def validate_host(value):
    """Accept only an explicit HTTPS origin, without credentials or a path.

    Main callers: config/state loading and website packaging. The exact origin
    binds operator credentials, SDK resources and the browser's CSP together.
    """
    value = required_value(value, "Foundry host")
    try:
        parsed = urlsplit(value)
        port = parsed.port
        valid = (parsed.scheme == "https" and parsed.hostname
                 and parsed.username is None and parsed.password is None
                 and not parsed.path and not parsed.query and not parsed.fragment
                 and not any(character.isspace() for character in value)
                 and re.fullmatch(r"[A-Za-z0-9.-]+(?::[0-9]+)?", parsed.netloc)
                 and all(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?", part)
                         for part in parsed.hostname.split("."))
                 and (port is None or 0 < port <= 65535))
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("Foundry host must be a valid HTTPS origin without credentials, path or query")
    return "https://" + parsed.hostname.lower() + (f":{port}" if port not in (None, 443) else "")


def _read_json(path, label):
    """Read an object and give a useful setup error without printing contents.

    Main callers: the config loader and generated-state readers.
    """
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ValueError(f"{label} is missing: {path}. Copy and complete its .example.json template first.") from None
    except (json.JSONDecodeError, UnicodeError):
        raise ValueError(f"{label} must be a valid JSON object: {path}") from None
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object: {path}")
    return value


def _section(value, allowed, required, name):
    """Validate optional settings only when the operator supplies that section.

    Main callers: load_deployment_config. No bootstrap identities or credential
    provider need to be configured for manual setup and token-free ZIP builds.
    """
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) - set(allowed):
        raise ValueError(f"{name} must contain only these fields: {', '.join(allowed)}")
    for key in required:
        required_value(value.get(key), f"{name}.{key}")
    return dict(value)


@dataclass(frozen=True)
class DeploymentConfig:
    """Validated origin, local state paths and optional operator services."""

    path: Path
    host: str
    deployment_state_path: Path
    resource_state_path: Path
    secret_manager: dict | None = None
    bootstrap: dict | None = None


def load_deployment_config(path=None):
    """Select a local config and resolve paths relative to that file.

    Main callers: operator commands and FoundryApi when no config was passed.
    Explicit --config wins over BACKPROP_DEPLOY_CONFIG, then the ignored default.
    Loading validates configuration only; it never retrieves a token or calls
    Foundry, so packaging and command help work without credentials.
    """
    selected = path if path is not None else os.environ.get("BACKPROP_DEPLOY_CONFIG", DEFAULT_CONFIG_PATH)
    if not selected:
        raise ValueError("BACKPROP_DEPLOY_CONFIG must name a local configuration file")
    selected = Path(selected).expanduser().resolve()
    data = _read_json(selected, "Deployment configuration")
    allowed = {"host", "deployment_state", "resource_state", "secret_manager", "bootstrap"}
    if set(data) - allowed:
        raise ValueError("Unknown deployment configuration fields: " + ", ".join(sorted(set(data) - allowed)))
    host = validate_host(data.get("host"))
    def state_path(key):
        value = required_value(data.get(key), key)
        return (selected.parent / Path(value).expanduser()).resolve()
    deployment_path = state_path("deployment_state")
    resource_path = state_path("resource_state")
    if deployment_path == resource_path or selected in (deployment_path, resource_path):
        raise ValueError("Config, deployment state and resource state must be distinct files")
    secret = _section(data.get("secret_manager"), ("project", "secret", "version"),
                      ("project", "secret"), "secret_manager")
    if secret is not None:
        secret["version"] = required_value(secret.get("version", "latest"), "secret_manager.version")
    bootstrap = _section(data.get("bootstrap"), ("space_rid", "organization_rid", "owner_user_id"),
                         ("space_rid", "organization_rid", "owner_user_id"), "bootstrap")
    return DeploymentConfig(selected, host, deployment_path, resource_path, secret, bootstrap)


def validate_state_binding(state, host, label):
    """Reject template IDs and existing state from a different enrollment.

    Main callers: state readers, bootstrap and provisioning before mutation.
    Empty state is valid only for creation; a saved nonempty state needs a host.
    """
    if not isinstance(state, dict):
        raise ValueError(f"{label} must be a JSON object")
    if state:
        state_host = validate_host(state.get("host"))
        if state_host != host:
            raise ValueError(f"{label} belongs to a different Foundry host")
        state["host"] = state_host
    for key in ("project_rid", "application_rid", "client_id", "stream_dataset_rid", "media_dataset_rid", "stream_view_rid", "app_url", "branch", "organization_rid"):
        if key in state:
            required_value(state[key], f"{label}.{key}")
    return state


def read_resource_state(config, path=None):
    """Read the completed SDK resource mapping for this installation.

    Main callers: deployment and verification commands after config selection.
    """
    selected = Path(path).expanduser().resolve() if path is not None else config.resource_state_path
    state = validate_state_binding(_read_json(selected, "Resource state"), config.host, "Resource state")
    for key in ("host", "stream_dataset_rid", "media_dataset_rid", "branch"):
        required_value(state.get(key), f"Resource state.{key}")
    return state


def read_optional_state(path, host, label):
    """Read prior creation progress, treating only an absent file as empty.

    Main callers: bootstrap and backend provisioning. Existing corrupt or wrong-
    enrollment state is an error, never permission to create duplicate resources.
    """
    path = Path(path)
    if not path.exists():
        return {}
    return validate_state_binding(_read_json(path, label), host, label)


def read_deployment_state(config, resources=None):
    """Read application handles and reject mixed application/resource settings.

    Main callers: deployment, domain/control configuration and verification.
    This protects the mutation boundary; public packaging repeats the shared
    checks so direct callers cannot bypass installation consistency.
    """
    state = validate_state_binding(_read_json(config.deployment_state_path, "Deployment state"), config.host, "Deployment state")
    required_value(state.get("application_rid"), "Deployment state.application_rid")
    required_value(state.get("client_id"), "Deployment state.client_id")
    organization = (config.bootstrap or {}).get("organization_rid")
    if organization is not None and "organization_rid" in state and state["organization_rid"] != organization:
        raise ValueError("Deployment state belongs to a different organization")
    if resources is not None:
        validate_matching_states(state, resources)
    return state


def validate_matching_states(state, resources):
    """Reject differing hosts/projects/callbacks instead of choosing one silently.

    Main callers: read_deployment_state and deploy.public_config.
    """
    for key in ("host", "project_rid", "app_url"):
        if key in state and key in resources:
            left, right = state[key], resources[key]
            if key == "host":
                left, right = validate_host(left), validate_host(right)
            if left != right:
                raise ValueError(f"Deployment and resource state have different {key} values")


def save_state(path, state):
    """Save local stage results with private filesystem permissions.

    Main callers: bootstrap, provisioning and deployment after verified stages.
    Saving after each successful resource creation lets a retry reuse its IDs.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".backprop-state-", delete=False) as handle:
            temporary = Path(handle.name)
            os.chmod(temporary, 0o600)
            handle.write(json.dumps(state, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
