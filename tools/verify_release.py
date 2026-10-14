"""Verify that every published website asset matches the local release archive.

Architecture: existing public deployment state -> deterministic archive -> fresh
HTTPS asset reads -> byte equality and digest report. This uses no credentials
and never reads experiment data. Authentication-gated hosting is reported as
unverifiable by this anonymous checker. Main callers: maintainers after deployment.
Why this exists: a deployed-version response alone cannot reveal missing modules
or an unexpectedly stale asset response at the actual application URL.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path
import sys
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "deploy"))
from deploy import build_archive
from local_config import load_deployment_config, read_deployment_state, read_resource_state


class ReleaseAuthenticationRequired(RuntimeError):
    """The hosting sign-in boundary prevents anonymous asset verification."""


class _AssetRedirectHandler(HTTPRedirectHandler):
    """Stop before following a hosting redirect, preserving the actual outcome.

    Main callers: verify's anonymous opener. Why this exists: urllib normally
    follows Foundry sign-in redirects and returns their HTML as if it were the
    requested JavaScript or app page, producing a misleading byte mismatch.
    """

    def redirect_request(self, request, response, code, message, headers, newurl):
        destination = urlsplit(newurl)
        components = set(destination.path.lower().strip("/").split("/"))
        auth_paths = {"multipass", "login", "signin", "sign-in", "auth", "oauth", "oauth2",
                      "sso", "saml", "saml2", "authorize", "authorization", "authentication"}
        auth_host = any(part in {"login", "auth", "sso"} for part in (destination.hostname or "").lower().split("."))
        if components & auth_paths or auth_host:
            raise ReleaseAuthenticationRequired(
                "Foundry hosting redirected the asset request to sign-in. "
                "Anonymous byte verification is unavailable; verify this deployment "
                "in an authenticated browser. This checker does not use browser credentials.")
        raise RuntimeError(
            f"Published asset request returned HTTP {code} redirect before verification. "
            "No asset bytes were compared; hosting authentication may be required.")


def read_published_asset(opener, url):
    """Read only the requested asset and distinguish access denial from mismatch.

    Main callers: verify. Redirect URLs and response bodies can contain login
    state, so failures report the boundary without copying that content.
    """
    request = Request(url, headers={"Cache-Control": "no-cache"})
    try:
        with opener.open(request, timeout=30) as response:
            return response.read()
    except HTTPError as error:
        if error.code in {401, 403}:
            error.close()
            raise ReleaseAuthenticationRequired(
                f"Foundry hosting denied anonymous asset access (HTTP {error.code}). "
                "Use an authenticated browser to verify this deployment; no asset bytes were compared.") from None
        raise


def verify(config_path=None):
    """Check configured release assets without obtaining an operator token.

    Main callers: the command entry point and isolated release tests. Both
    state files must belong to the configured host before any network read.
    """
    config = load_deployment_config(config_path)
    resources = read_resource_state(config)
    state = read_deployment_state(config, resources)
    archive = build_archive(state, resources)
    digest = hashlib.sha256(archive).hexdigest()
    if digest != state["archive_sha256"]:
        raise ValueError("Local public assets differ from the recorded deployed archive")
    names = []
    opener = build_opener(_AssetRedirectHandler())
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        for name in bundle.namelist():
            url = state["app_url"].rstrip("/") + "/" + name + "?" + urlencode({"verify": state["deployed_version"]})
            data = read_published_asset(opener, url)
            if data != bundle.read(name):
                raise ValueError(f"Published asset does not match the release: {name}")
            names.append(name)
    return {"version": state["deployed_version"], "url": state["app_url"],
            "verified_assets": len(names), "archive_sha256": digest}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, help="Operator config; defaults to BACKPROP_DEPLOY_CONFIG")
    args = parser.parse_args()
    try:
        result = verify(args.config)
    except ReleaseAuthenticationRequired as error:
        print(json.dumps({"status": "authentication_required", "message": str(error)}, indent=2), file=sys.stderr)
        raise SystemExit(2)
    print(json.dumps(result, indent=2))
