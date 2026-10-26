"""
Purpose: Deploy isolated Cloud Run, Firestore, and GCS resources with scoped cleanup.
Input: Explicit Google project settings and authenticated gcloud credentials.
Output: A resource ledger and a credential-free SDK connection file.
Note: Cleanup removes only resources successfully created by this invocation.
Note: The project must already have the required APIs and IAM permissions.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import re
import subprocess
import tempfile
import time
from typing import Callable


ROOT = Path(__file__).resolve().parents[1]
REQUIRED_APIS = (
    "artifactregistry.googleapis.com",
    "cloudbuild.googleapis.com",
    "firestore.googleapis.com",
    "iam.googleapis.com",
    "run.googleapis.com",
    "storage.googleapis.com",
)
# The Firestore IAM binding uses an exact database-name condition. The script
# never enables APIs or grants database access without that resource condition.
REQUIRED_DEPLOYER_PERMISSIONS = (
    "artifactregistry.dockerimages.get",
    "artifactregistry.repositories.create",
    "artifactregistry.repositories.delete",
    "artifactregistry.repositories.downloadArtifacts",
    "artifactregistry.repositories.get",
    "cloudbuild.builds.create",
    "cloudbuild.builds.get",
    "cloudbuild.builds.list",
    "cloudbuild.operations.get",
    "datastore.databases.create",
    "datastore.databases.delete",
    "datastore.databases.getMetadata",
    "datastore.indexes.create",
    "datastore.indexes.get",
    "datastore.indexes.list",
    "datastore.indexes.update",
    "datastore.operations.get",
    "iam.serviceAccounts.actAs",
    "iam.serviceAccounts.create",
    "iam.serviceAccounts.delete",
    "iam.serviceAccounts.get",
    "resourcemanager.projects.get",
    "resourcemanager.projects.getIamPolicy",
    "resourcemanager.projects.setIamPolicy",
    "run.operations.get",
    "run.services.create",
    "run.services.delete",
    "run.services.get",
    "run.services.list",
    "run.services.setIamPolicy",
    "run.services.update",
    "serviceusage.services.use",
    "storage.buckets.create",
    "storage.buckets.delete",
    "storage.buckets.get",
    "storage.buckets.getIamPolicy",
    "storage.buckets.setIamPolicy",
    "storage.buckets.update",
    "storage.objects.create",
    "storage.objects.delete",
    "storage.objects.get",
    "storage.objects.list",
)
REQUIRED_BUILD_PERMISSIONS = (
    "artifactregistry.repositories.downloadArtifacts",
    "artifactregistry.repositories.uploadArtifacts",
    "storage.buckets.get",
    "storage.objects.create",
    "storage.objects.get",
    "storage.objects.list",
)
REQUIRED_RUNTIME_PERMISSIONS = (
    "storage.objects.create",
    "storage.objects.delete",
    "storage.objects.get",
    "storage.objects.list",
    "storage.objects.update",
)
OAUTH_SETUP_ROLE = "roles/oauthconfig.editor"
REQUIRED_FIRESTORE_PERMISSIONS = (
    "datastore.databases.get",
    "datastore.entities.create",
    "datastore.entities.delete",
    "datastore.entities.get",
    "datastore.entities.list",
    "datastore.entities.update",
)


def deployment_requirements() -> dict:
    """
    Purpose: Describe project prerequisites without Google credentials or mutations.
    Input: None.
    Output: Required APIs and permissions for each deployment identity.
    """
    return {
        "enabled_apis": list(REQUIRED_APIS),
        "deployer": {
            "scope": "Selected project and this installation's resources",
            "permissions": list(REQUIRED_DEPLOYER_PERMISSIONS),
        },
        "cloud_build": {
            "scope": "Staging/log bucket and image repository",
            "permissions": list(REQUIRED_BUILD_PERMISSIONS),
        },
        "runtime": {
            "scope": "Installation bucket; the script grants roles/storage.objectAdmin",
            "permissions": list(REQUIRED_RUNTIME_PERMISSIONS),
        },
        "runtime_firestore": {
            "scope": "Only the new named database, enforced by an IAM condition",
            "permissions": list(REQUIRED_FIRESTORE_PERMISSIONS),
            "role": "roles/datastore.user",
        },
        "service_agents": {
            "cloud_build": "roles/cloudbuild.serviceAgent",
            "cloud_run": "roles/run.serviceAgent",
        },
        "browser_authentication": (
            "Google requires manual creation and configuration of an OAuth Web "
            "client. IAM permissions do not permit API-based client creation."
        ),
        "oauth_setup_role": OAUTH_SETUP_ROLE,
    }


def deploy(config: dict, state_path: Path, *, run: Callable = subprocess.run) -> dict:
    """
    Purpose: Create dedicated resources with a least-privilege runtime identity.
    Input: Operator settings and a new private ledger path.
    Output: A deployed service, its connection, and the remaining manual OAuth steps.
    """
    config = _validate_config(config)
    _validate_browser_auth(config)
    if state_path.exists():
        raise ValueError("Use a new state path; an existing resource ledger must be preserved")
    project, region, name = (config[key] for key in ("project", "region", "name"))
    bucket = config.get("bucket", f"{project}-{name}")
    account_id = name[:30]
    account = f"{account_id}@{project}.iam.gserviceaccount.com"
    database = config.get("firestore_database", name)
    state = {"config": config, "bucket": bucket, "account": account,
             "database": database, "owned": [],
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
            prefix = config.get("prefix", "backprop").rstrip("/")
            lifecycle.write_text(json.dumps({"rule": [{"action": {"type": "Delete"},
                "condition": {"age": 1, "matchesPrefix": [prefix + "/uploads/", prefix + "/queries/"]}}]}))
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
        create("database", "firestore", "databases", "create", "--database", database,
               "--location", config.get("firestore_location", region),
               "--type", "firestore-native", "--edition", "standard",
               "--concurrency-mode", "pessimistic", "--no-delete-protection")
        # Google exposes per-database access through a conditional project policy.
        # The new runtime identity cannot have an earlier binding to this database.
        state["owned"].append("firestore_access")
        _save(state_path, state)
        for attempt in range(8):
            try:
                command("projects", "add-iam-policy-binding", project,
                        "--member", "serviceAccount:" + account, "--role", "roles/datastore.user",
                        "--condition", _firestore_condition(project, database))
                break
            except subprocess.CalledProcessError:
                if attempt == 7:
                    raise
                time.sleep(min(2 ** attempt, 10))
        _configure_firestore(command, database)
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
            environment = {"BACKPROP_GCP_PROJECT": project,
                           "BACKPROP_FIRESTORE_DATABASE": database,
                           "BACKPROP_GCS_BUCKET": bucket,
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
                    "--cpu", "1", "--cpu-throttling", "--concurrency", "4", "--min-instances", "0",
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
        if connection.get("client_id", "") != config.get("client_id", ""):
            raise RuntimeError("The deployed Google OAuth client ID does not match the configuration")
        connection_path = state_path.with_name(state_path.stem + ".connection.json")
        _save(connection_path, connection)
        state.update(status="deployed", origin=origin,
                     browser_authentication=_browser_setup(config, origin),
                     connection_path=str(connection_path.resolve()))
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
        "database": ["firestore", "databases", "delete", "--database", state.get("database", config["name"])],
        "firestore_access": ["projects", "remove-iam-policy-binding", config["project"],
            "--member", "serviceAccount:" + state["account"], "--role", "roles/datastore.user",
            "--condition", _firestore_condition(config["project"], state.get("database", config["name"]))],
    }
    failures = []
    for resource in reversed(list(state["owned"])):
        result = run(["gcloud", *commands[resource], "--project", config["project"], "--quiet"],
                     text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        removed = result.returncode == 0
        if not removed and resource == "firestore_access":
            removed = _firestore_binding_absent(state, run=run)
        if not removed:
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


def _configure_firestore(command: Callable, database: str) -> None:
    """
    Purpose: Wait for all required index and TTL configurations before deployment.
    Input: The scoped gcloud command runner and newly owned database.
    Output: Ready indexes with independent configuration operations in bounded parallel groups.
    """
    exemptions = [
        ["firestore", "indexes", "fields", "update", "*", "--database", database,
         "--collection-group", collection, "--disable-indexes"]
        for collection in ("events", "partitions", "files", "queries")
    ]
    indexes = []
    for fields in json.loads((ROOT / "deploy/firestore.indexes.json").read_text())["indexes"]:
        arguments = ["firestore", "indexes", "composite", "create", "--database", database,
                     "--collection-group", "events", "--query-scope", "collection"]
        for field in fields:
            arguments.extend(["--field-config", field])
        indexes.append(arguments)
    indexes.append(["firestore", "fields", "ttls", "update", "expires_at", "--database", database,
                    "--collection-group", "queries", "--enable-ttl"])
    for operations in (exemptions, indexes):
        with ThreadPoolExecutor(max_workers=4) as workers:
            futures = [workers.submit(command, *arguments) for arguments in operations]
            for future in futures:
                future.result()


def _browser_setup(config: dict, origin: str) -> dict:
    """
    Purpose: Explain the manual OAuth steps for the new service's exact origin.
    Input: A validated deployment configuration and the assigned Cloud Run origin.
    Output: Registration instructions or an explicit SDK-only result.
    """
    if config.get("sdk_only", False):
        return {"status": "sdk_only"}
    return {
        "status": "origin_registration_and_sign_in_required",
        "client_id": config["client_id"],
        "authorized_javascript_origin": origin,
        "clients_url": (
            "https://console.cloud.google.com/auth/clients?project=" + config["project"]
        ),
        "instructions": (
            "Add this exact origin to the Web client's Authorized JavaScript "
            "origins, save, then open the origin and test Sign in with Google. "
            "See docs/gcp-setup.md#4-register-the-origin-and-verify-sign-in."
        ),
    }


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
    for field in ("firestore_database", "firestore_location"):
        if field in config and (not isinstance(config[field], str)
                or not re.fullmatch(r"[a-z][a-z0-9-]{2,61}[a-z0-9]", config[field])):
            raise ValueError(f"Set a valid explicit {field}; the default database is not adopted")
    return dict(config)


def _validate_browser_auth(config: dict) -> None:
    """
    Purpose: Reject incomplete browser setup before the first cloud resource exists.
    Input: A Web OAuth client ID or an explicit SDK-only selection.
    Output: No value, or an actionable configuration error.
    Note: Cleanup accepts older ledgers without a browser client ID.
    """
    if type(config.get("sdk_only", False)) is not bool:
        raise ValueError("sdk_only must be true or false")
    client_id = config.get("client_id", "")
    if config.get("sdk_only", False) and client_id == "":
        return
    if config.get("sdk_only", False):
        raise ValueError("Remove client_id for an SDK-only deployment")
    if (not isinstance(client_id, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]+\.apps\.googleusercontent\.com", client_id)):
        raise ValueError(
            "Set client_id to a Google OAuth Web application client ID. "
            "Follow docs/gcp-setup.md#2-one-time-google-oauth-setup. "
            "For intentional SDK-only use, set sdk_only to true and omit client_id."
        )


def _firestore_condition(project: str, database: str) -> str:
    return (f'expression=resource.name=="projects/{project}/databases/{database}",'
            f'title=backprop-{database}')


def _firestore_binding_absent(state: dict, *, run: Callable) -> bool:
    """
    Purpose: Confirm cleanup after an IAM grant failed before its binding existed.
    Input: The owned account, exact database condition, and gcloud runner.
    Output: True only when a successful policy read proves that binding absent.
    """
    project = state["config"]["project"]
    database = state.get("database", state["config"]["name"])
    result = run(["gcloud", "projects", "get-iam-policy", project,
                  "--format", "json", "--project", project, "--quiet"],
                 text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        return False
    expression = f'resource.name=="projects/{project}/databases/{database}"'
    return not any(
        binding.get("role") == "roles/datastore.user"
        and "serviceAccount:" + state["account"] in binding.get("members", [])
        and binding.get("condition", {}).get("expression") == expression
        and binding.get("condition", {}).get("title") == f"backprop-{database}"
        for binding in json.loads(result.stdout).get("bindings", [])
    )


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
    parser.add_argument("--state", type=Path, help="Private resource ledger path")
    parser.add_argument("--cleanup", action="store_true", help="Delete only resources owned by this ledger")
    parser.add_argument(
        "--requirements", action="store_true",
        help="Print required APIs and IAM permissions without cloud operations",
    )
    args = parser.parse_args()
    if args.requirements:
        print(json.dumps(deployment_requirements(), indent=2))
        return
    if not args.state:
        parser.error("--state is required for deployment or cleanup")
    if args.cleanup:
        state = cleanup(args.state)
    else:
        if not args.config:
            parser.error("--config is required for deployment")
        state = deploy(json.loads(args.config.read_text()), args.state)
    fields = ("status", "owned", "origin", "browser_authentication")
    print(json.dumps({key: state[key] for key in fields if key in state}, indent=2))


if __name__ == "__main__":
    main()
