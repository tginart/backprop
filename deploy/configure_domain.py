"""Bind an approved Foundry website domain to Backprop's public OAuth client.

Architecture: verify the app's registered domain -> set its exact OAuth callback
-> update non-secret deployment manifests. Domain request/approval itself uses
Foundry's normal UI. This script never requests unrestricted data permissions.
Main callers: operator CLI after initial domain approval.
"""
import argparse
import json
from pathlib import Path
from urllib.parse import urlsplit

from foundry_api import FoundryApi
from local_config import load_deployment_config, read_deployment_state, read_resource_state, save_state

ROOT = Path(__file__).resolve().parents[1]


def main():
    """Configure only an HTTPS root URL already registered to this application.

    Main callers: command-line entry point. The explicit domain check prevents
    accidentally redirecting the app's authorization codes to an unrelated site.
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--app-url", required=True)
    parser.add_argument("--config", type=Path, help="Local deployment configuration")
    args = parser.parse_args()
    url = urlsplit(args.app_url)
    if url.scheme != "https" or not url.hostname or url.path not in ("", "/") or url.query or url.fragment or url.username or url.port:
        parser.error("Use the exact HTTPS root URL of the registered Foundry website")
    config = load_deployment_config(args.config)
    resources = read_resource_state(config)
    state = read_deployment_state(config, resources)
    api = FoundryApi(config)
    website = api.request("/api/v2/thirdPartyApplications/" + state["application_rid"] + "/website")
    if url.hostname not in website.get("subdomains", []):
        raise ValueError("This domain is not registered to the Backprop application")
    app_url = "https://" + url.hostname + "/"
    api.request("/third-party-application-service/api/applications/" + state["application_rid"] + "/client-config", "PUT", {
        "clientSpecification": {"type": "public", "public": {
            "authorizationCodeGrant": {"enabled": True, "redirectUrls": [app_url]},
            "refreshTokenGrant": {"enabled": True},
        }},
    })
    state.update(app_url=app_url, subdomains=website["subdomains"], domain_approved=True)
    save_state(config.deployment_state_path, state)
    resources["app_url"] = app_url
    save_state(config.resource_state_path, resources)
    print("Configured verified domain and OAuth callback:", app_url)


if __name__ == "__main__":
    main()
