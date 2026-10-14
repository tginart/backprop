"""Foundry HTTP client for deployment and provisioning commands.

Reads an operator token from the environment or Google Cloud Secret Manager.
Requests stay on the configured Foundry origin; redirects are rejected and
credentials are neither printed nor persisted by this client.
"""

import json
import os
import subprocess
import urllib.error
import urllib.request
from local_config import load_deployment_config, validate_host


def _token(value):
    """Reject missing or header-unsafe credentials without exposing their value.

    Main callers: FoundryApi credential selection. An explicitly empty env value
    is a setup error, not permission to try a different credential source.
    """
    if not isinstance(value, str) or not value.strip() or any(character.isspace() for character in value):
        raise ValueError("Foundry token must be nonempty and contain no whitespace")
    return value


def operator_token(config):
    """Select one credential source, keeping provider output out of diagnostics.

    Main callers: FoundryApi. Environment tokens take precedence in the documented
    order; Google Cloud is used only when the local config explicitly requests it.
    """
    for name in ("BACKPROP_TOKEN", "FOUNDRY_TOKEN"):
        if name in os.environ:
            return _token(os.environ[name])
    secret = config.secret_manager
    if secret is None:
        raise ValueError("Set BACKPROP_TOKEN or FOUNDRY_TOKEN, or configure secret_manager in the local deployment config")
    try:
        result = subprocess.run(
            ["gcloud", "secrets", "versions", "access", secret["version"],
             "--secret=" + secret["secret"], "--project=" + secret["project"]],
            capture_output=True, text=True, timeout=30, check=True,
        )
    except (OSError, subprocess.SubprocessError):
        raise RuntimeError("Could not load the configured Foundry credential from Secret Manager") from None
    return _token(result.stdout.rstrip("\r\n"))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Keep bearer credentials on the explicitly selected Foundry host."""

    def redirect_request(self, *args, **kwargs):
        return None


class FoundryApi:
    """Read the existing secret once, then send authenticated API requests.

    Main callers: deployment scripts and read-only capability probes.
    """

    def __init__(self, config=None, *, token=None):
        self.config = load_deployment_config() if config is None else config
        self.host = validate_host(self.config.host)
        self.token = operator_token(self.config) if token is None else _token(token)
        self.opener = urllib.request.build_opener(NoRedirect)

    def request(self, path, method="GET", body=None, content_type="application/json", *, timeout=30):
        """Send one request and return decoded JSON or the raw response bytes.

        Used by deployment and provisioning commands.
        An HTTP failure raises; callers must explicitly handle the exact error.
        """
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError("Use a relative path on the configured Foundry host")
        if body is not None and not isinstance(body, bytes):
            body = json.dumps(body).encode()
        request = urllib.request.Request(
            self.host + path, data=body, method=method,
            headers={"Authorization": "Bearer " + self.token,
                     "Content-Type": content_type},
        )
        with self.opener.open(request, timeout=timeout) as response:
            data = response.read()
            if "json" in response.headers.get("Content-Type", ""):
                return json.loads(data) if data else None
            return data


def redact(value):
    """Remove credential fields from API responses before showing diagnostics.

    Main callers: capability probes; deployment state contains selected IDs only.
    """
    if isinstance(value, dict):
        return {key: "[redacted]" if any(word in key.lower() for word in
                ["secret", "token", "password", "credential"]) else redact(item)
                for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value
