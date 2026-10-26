"""
Purpose: Deploy an isolated Cloud Run/GCS installation and remove its owned resources.
Input: Explicit Google project settings and authenticated gcloud credentials.
Output: A resource ledger and a credential-free SDK connection file.
Note: Cleanup removes only resources successfully created by this invocation.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
import tempfile
import time
from typing import Callable


ROOT = Path(__file__).resolve().parents[1]


def deploy(config: dict, state_path: Path, *, run: Callable = subprocess.run) -> dict:
    """
    Purpose: Create dedicated resources with a least-privilege runtime identity.
    Input: Operator settings and a new private ledger path.
    Output: A ready service and connection without changes to existing installations.
    """
    config = _validate_config(config)
    if state_path.exists():
        raise ValueError("Use a new state path; an existing resource ledger must be preserved")
    project, region, name = (config[key] for key in ("project", "region", "name"))
    bucket = config.get("bucket", f"{project}-{name}")
    account_id = name[:30]
    account = f"{account_id}@{project}.iam.gserviceaccount.com"
    state = {"config": config, "bucket": bucket, "account": account, "owned": [],
             "status": "creating"}
    _save(state_path, state)

    def command(*arguments: str) -> str:
        result = run(["gcloud", *arguments, "--project", project, "--quiet"],
                     check=True, text=True, stdout=subprocess.PIPE)
        return result.stdout.strip()

    def create(resource: str, *arguments: str) -> None:
        command(*arguments)
        state["owned"].append(resource)
        _save(state_path, state)

    try:
        create("bucket", "storage", "buckets", "create", "gs://" + bucket,
               "--location", region, "--uniform-bucket-level-access", "--public-access-prevention",
               "--soft-delete-duration=0")
        with tempfile.TemporaryDirectory(prefix="backprop-gcs-policy-") as directory:
            lifecycle = Path(directory) / "lifecycle.json"
            lifecycle.write_text(json.dumps({"rule": [{"action": {"type": "Delete"},
                "condition": {"age": 1, "matchesPrefix": [config.get("prefix", "backprop").rstrip("/") + "/uploads/"]}}]}))
            command("storage", "buckets", "update", "gs://" + bucket,
                    "--lifecycle-file", str(lifecycle))
        create("account", "iam", "service-accounts", "create", account_id)
        for attempt in range(8):
            try:
                command("storage", "buckets", "add-iam-policy-binding", "gs://" + bucket,
                        "--member", "serviceAccount:" + account, "--role", "roles/storage.objectAdmin")
                break
            except subprocess.CalledProcessError:
                if attempt == 7:
                    raise
                time.sleep(min(2 ** attempt, 10))
        create("repository", "artifacts", "repositories", "create", name,
               "--location", region, "--repository-format", "docker")
        image = f"{region}-docker.pkg.dev/{project}/{name}/backprop:deployment"
        state["image"] = image
        _save(state_path, state)
        with tempfile.TemporaryDirectory(prefix="backprop-cloud-build-") as directory:
            build = Path(directory) / "build.json"
            build.write_text(json.dumps({"steps": [{"name": "gcr.io/cloud-builders/docker",
                "args": ["build", "-f", "deploy/Dockerfile.gcp", "-t", image, "."]}],
                "images": [image]}))
            command("builds", "submit", str(ROOT), "--config", str(build),
                    "--gcs-source-staging-dir", f"gs://{bucket}/build-source",
                    "--gcs-log-dir", f"gs://{bucket}/build-logs")
            env = Path(directory) / "environment.json"
            environment = {"BACKPROP_GCS_BUCKET": bucket,
                           "BACKPROP_GCS_PREFIX": config.get("prefix", "backprop"),
                           "BACKPROP_GCP_ORIGIN": "https://bootstrap.invalid",
                           "BACKPROP_GOOGLE_CLIENT_ID": config.get("client_id", ""),
                           "BACKPROP_GOOGLE_ALLOWED_EMAILS": ",".join(config["allowed_emails"])}
            env.write_text(json.dumps(environment))
            # A failed deploy can still create a service. Record ownership first,
            # after a separate existence check rules out an existing installation.
            existing = command("run", "services", "list", "--region", region,
                               "--filter", "metadata.name=" + name, "--format", "value(metadata.name)")
            if existing:
                raise ValueError("The Cloud Run service already exists; choose a new name")
            state["owned"].append("service")
            _save(state_path, state)
            command("run", "deploy", name, "--region", region, "--image", image,
                    "--service-account", account, "--no-invoker-iam-check",
                    "--env-vars-file", str(env), "--port", "8080", "--memory", "4Gi",
                    "--cpu", "1", "--concurrency", "4", "--min-instances", "0",
                    "--max-instances", str(config.get("max_instances", 4)), "--timeout", "300")
            origin = command("run", "services", "describe", name, "--region", region,
                             "--format", "value(status.url)")
            environment["BACKPROP_GCP_ORIGIN"] = origin
            env.write_text(json.dumps(environment))
            command("run", "services", "update", name, "--region", region,
                    "--env-vars-file", str(env))
        from urllib.request import urlopen
        with urlopen(origin + "/config.json", timeout=60) as response:
            connection = json.load(response)
        connection_path = state_path.with_name(state_path.stem + ".connection.json")
        _save(connection_path, connection)
        state.update(status="ready", origin=origin, connection_path=str(connection_path.resolve()))
        _save(state_path, state)
        return state
    except BaseException:
        state["status"] = "failed"
        _save(state_path, state)
        raise


def cleanup(state_path: Path, *, run: Callable = subprocess.run) -> dict:
    """
    Purpose: Remove only resources in a dedicated deployment's ownership ledger.
    Input: The saved ledger from deploy.
    Output: Updated cleanup evidence, with failed removals retained for a retry.
    """
    state = json.loads(state_path.read_text())
    config = _validate_config(state["config"])
    commands = {
        "service": ["run", "services", "delete", config["name"], "--region", config["region"]],
        "repository": ["artifacts", "repositories", "delete", config["name"], "--location", config["region"]],
        "account": ["iam", "service-accounts", "delete", state["account"]],
        "bucket": ["storage", "rm", "--recursive", "gs://" + state["bucket"]],
    }
    failures = []
    for resource in reversed(list(state["owned"])):
        result = run(["gcloud", *commands[resource], "--project", config["project"], "--quiet"],
                     text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if result.returncode:
            failures.append(resource)
        else:
            state["owned"].remove(resource)
            _save(state_path, state)
    state["status"] = "cleanup_failed" if failures else "deleted"
    state["cleaned_at_unix_s"] = int(time.time())
    _save(state_path, state)
    if failures:
        raise RuntimeError("Cleanup failed for: " + ", ".join(failures) + "; retry the same ledger")
    return state


def _validate_config(config: dict) -> dict:
    """
    Purpose: Require explicit deployment identities before any cloud operation.
    Input: Operator-selected resource names, Google emails, and scale limits.
    Output: A detached configuration or an immediate validation error.
    """
    for field in ("project", "region", "name"):
        if not isinstance(config.get(field), str) or not re.fullmatch(r"[a-z][a-z0-9-]{2,28}[a-z0-9]", config[field]):
            raise ValueError(f"Set an explicit valid {field}; use at most 30 characters")
    if not config.get("allowed_emails") or not isinstance(config["allowed_emails"], list):
        raise ValueError("Configure a nonempty allowed_emails list")
    if any(not isinstance(email, str) or "@" not in email for email in config["allowed_emails"]):
        raise ValueError("Each allowed identity must be an email")
    if type(config.get("max_instances", 4)) is not int or not 1 <= config.get("max_instances", 4) <= 100:
        raise ValueError("max_instances must be between 1 and 100")
    return dict(config)


def _save(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n")


def main() -> None:
    """
    Purpose: Select deployment or cleanup through one explicit resource ledger.
    Input: CLI paths and an optional cleanup request.
    Output: A compact deployment or cleanup status.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, help="Explicit deployment JSON; see gcp.example.json")
    parser.add_argument("--state", type=Path, required=True, help="Private resource ledger path")
    parser.add_argument("--cleanup", action="store_true", help="Delete only resources owned by this ledger")
    args = parser.parse_args()
    if args.cleanup:
        state = cleanup(args.state)
    else:
        if not args.config:
            parser.error("--config is required for deployment")
        state = deploy(json.loads(args.config.read_text()), args.state)
    print(json.dumps({key: state[key] for key in ("status", "owned", "origin") if key in state}, indent=2))


if __name__ == "__main__":
    main()
