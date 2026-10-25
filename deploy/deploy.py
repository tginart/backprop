"""Package and publish Backprop's static frontend to Foundry.

Architecture: public resource config -> allowlisted website files -> versioned
archive upload -> malware scan -> deployment verification. Credentials stay in
memory. SDK/backend source, data files, and operator state are never uploaded.
Main callers: operator CLI for initial releases and subsequent updates.
"""
import argparse
import hashlib
import html
import io
import json
from pathlib import Path
import re
import sys
import time
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
import zipfile

from foundry_api import FoundryApi
from local_config import (load_deployment_config, read_deployment_state, read_resource_state,
                          required_value, save_state, validate_host, validate_matching_states)
from map_policy import MAKI_ICON_SOURCE, map_resource_origins

ROOT = Path(__file__).resolve().parents[1]
PUBLIC_KEYS = {"host", "stream_dataset_rid", "media_dataset_rid", "stream_view_rid", "branch"}
sys.path.insert(0, str(ROOT))
from backend.assets import asset_names, source_asset

PUBLIC_ASSETS = tuple(asset_names())
MAX_WEBSITE_FILES = 1000
MAX_WEBSITE_BYTES = 20_000_000
ZIP_TIMESTAMP = (2026, 1, 1, 0, 0, 0)


def public_config(state, resources):
    """Copy only non-secret fields needed by the browser and SDK setup screen.

    Main callers: build_archive. An explicit key allowlist prevents accidental
    future operator settings from entering the published website.
    """
    config = {key: resources[key] for key in PUBLIC_KEYS if key in resources}
    validate_matching_states(state, resources)
    config.update(backend="foundry", client_id=state.get("client_id"), app_url=state.get("app_url"))
    config["map_resource_origins"] = map_resource_origins(state)
    config["host"] = validate_host(config.get("host"))
    for key in ("stream_dataset_rid", "media_dataset_rid", "branch", "client_id", "app_url"):
        required_value(config.get(key), f"Public configuration requires {key}")
    app_url = urlsplit(config["app_url"])
    if (app_url.scheme != "https" or not app_url.hostname or app_url.username
            or app_url.path not in ("", "/") or app_url.query or app_url.fragment):
        raise ValueError("app_url must be an HTTPS website origin without credentials or query parameters")
    return config


def apply_content_security_policy(source, config):
    """Add a release-specific browser resource policy before page content loads.

    Main callers: build_archive. The app may read its own assets, authenticated
    Foundry data, local media blobs and configured map providers. Logged HTML
    has its own restrictive policy and script-disabled sandbox in media-view.
    """
    providers = " ".join([*map_resource_origins(config), MAKI_ICON_SOURCE])
    policy = "; ".join((
        "default-src 'self'", "script-src 'self'", "style-src 'self' 'unsafe-inline'",
        f"connect-src 'self' blob: {config['host']} {providers}", f"img-src 'self' blob: data: {providers}",
        "media-src 'self' blob:", "frame-src 'self' blob:", "object-src 'none'",
        "worker-src 'self' blob:", "base-uri 'self'", "form-action 'none'",
    ))
    meta = '<meta http-equiv="Content-Security-Policy" content="' + html.escape(policy, quote=True) + '">'
    document = source.decode("utf-8")
    document, count = re.subn(r"(<head(?:\s[^>]*)?>)", lambda match: match[0] + "\n" + meta,
                             document, count=1, flags=re.IGNORECASE)
    if count != 1:
        raise ValueError("index.html requires a head element for its content security policy")
    return document.encode("utf-8")


def archive_entry(name):
    """Use identical ZIP metadata for both authored and generated public files."""
    info = zipfile.ZipInfo(name, ZIP_TIMESTAMP)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = 0o100644 << 16
    return info


def build_archive(state, resources):
    """Build a reproducible ZIP of public assets with generated configuration.

    Main callers: main and deployment checks. No hidden files or symlinks enter
    the archive. File contents are bounded to Foundry's website hosting limit.
    """
    web = ROOT / "web"
    config = public_config(state, resources)
    root_assets = ["THIRD_PARTY_NOTICES.md"] + (["LICENSE", "NOTICE"] if (ROOT / "LICENSE").is_file() else [])
    if len(PUBLIC_ASSETS) != len(set(PUBLIC_ASSETS)):
        raise ValueError("Duplicate website assets")
    if len(PUBLIC_ASSETS) + len(root_assets) + 1 > MAX_WEBSITE_FILES:
        raise ValueError("Website exceeds Foundry's 1,000 file limit")
    total = 0
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as bundle:
        for name in sorted(PUBLIC_ASSETS):
            path = source_asset(name, ROOT)
            if path.is_symlink() or not path.is_file():
                raise ValueError(f"Required public asset must be a regular file: {name}")
            data = path.read_bytes()
            if name == "index.html":
                data = apply_content_security_policy(data, config)
            total += len(data)
            if total > MAX_WEBSITE_BYTES:
                raise ValueError("Website exceeds Foundry's 20 MB aggregate limit")
            if len(data) > MAX_WEBSITE_BYTES:
                raise ValueError("Website asset exceeds Foundry's 20 MB file limit")
            bundle.writestr(archive_entry(name), data)
        for name in root_assets:
            path = ROOT / name
            if path.is_symlink() or not path.is_file():
                raise ValueError("Required notice must be a regular file: " + name)
            data = path.read_bytes()
            total += len(data)
            if total > MAX_WEBSITE_BYTES:
                raise ValueError("Website exceeds Foundry's 20 MB aggregate limit")
            bundle.writestr(archive_entry(name), data)
        config_bytes = json.dumps(config, sort_keys=True, separators=(",", ":"))
        if total + len(config_bytes.encode()) > MAX_WEBSITE_BYTES:
            raise ValueError("Website exceeds Foundry's 20 MB aggregate limit")
        bundle.writestr(archive_entry("config.json"), config_bytes)
    if len(stream.getvalue()) > MAX_WEBSITE_BYTES:
        raise ValueError("Website ZIP exceeds Foundry's 20 MB aggregate limit")
    return stream.getvalue()


def main():
    """Upload an immutable version, wait for scanning, and verify the live app.

    Main callers: command-line entry point. Only an explicit ScanningInProgress
    response is retried. Other failures stop the deployment with a clear error.
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True)
    parser.add_argument("--build-only", type=Path)
    parser.add_argument("--config", type=Path, help="Local deployment config (or BACKPROP_DEPLOY_CONFIG)")
    args = parser.parse_args()
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?", args.version):
        parser.error("Use a semantic version, for example 0.1.0")
    config = load_deployment_config(args.config)
    resources = read_resource_state(config)
    state = read_deployment_state(config, resources)
    archive = build_archive(state, resources)
    if args.build_only:
        args.build_only.write_bytes(archive)
        print("Built website ZIP:", args.build_only)
        return
    api = FoundryApi(config)
    base = "/api/v2/thirdPartyApplications/" + state["application_rid"] + "/website"
    result = api.request(base + "/versions/upload?" + urlencode({"version": args.version}),
                         "POST", archive, "application/octet-stream", timeout=180)
    if result["version"] != args.version:
        raise RuntimeError("Foundry did not confirm the uploaded version")
    print("Uploaded", args.version, flush=True)
    deadline = time.monotonic() + 180
    while True:
        try:
            api.request(base + "/deploy", "POST", {"version": args.version})
            break
        except HTTPError as error:
            data = json.loads(error.read())
            if "ScanningInProgress" not in data.get("errorName", "") or time.monotonic() > deadline:
                raise RuntimeError("Website deployment failed: " + data.get("errorName", str(error.code))) from None
            print("Waiting for Foundry's asset scan", flush=True)
            time.sleep(5)
    website = api.request(base)
    if website.get("deployedVersion") != args.version:
        raise RuntimeError("Foundry did not confirm the live version")
    state.update(deployed_version=args.version, archive_sha256=hashlib.sha256(archive).hexdigest(),
                 subdomains=website.get("subdomains", []))
    save_state(config.deployment_state_path, state)
    print("Deployed and verified", args.version, state["app_url"])


if __name__ == "__main__":
    main()
