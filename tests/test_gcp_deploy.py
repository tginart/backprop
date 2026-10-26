"""
Purpose: Verify configurable GCP deployment and ownership-scoped cleanup commands.
Input: Synthetic gcloud outcomes and a temporary resource ledger.
Output: No implicit project, unrelated deletion, or credential-bearing connection.
"""
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("gcp_deployment", ROOT / "deploy/gcp_deploy.py")
DEPLOY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DEPLOY)
CONFIG = {"project": "customer-project", "region": "europe-west1", "name": "backprop-test",
          "allowed_emails": ["operator@example.com"], "prefix": "isolated",
          "client_id": "123456-example.apps.googleusercontent.com"}


class GcpDeploymentTests(unittest.TestCase):
    """Exercise deployment and failure ledgers without Google credentials."""

    def test_requirements_need_no_config_ledger_or_google_credentials(self):
        output = io.StringIO()
        with patch.object(sys, "argv", ["gcp_deploy.py", "--requirements"]), \
                patch("subprocess.run", side_effect=AssertionError("Cloud call")), \
                patch("sys.stdout", output):
            DEPLOY.main()
        requirements = json.loads(output.getvalue())
        self.assertIn("run.googleapis.com", requirements["enabled_apis"])
        permissions = requirements["deployer"]["permissions"]
        self.assertIn("iam.serviceAccounts.actAs", permissions)
        self.assertIn("storage.buckets.setIamPolicy", permissions)
        self.assertNotIn("serviceusage.services.enable", permissions)
        self.assertIn("resourcemanager.projects.setIamPolicy", permissions)
        self.assertIn("firestore.googleapis.com", requirements["enabled_apis"])
        self.assertIn("datastore.entities.create", requirements["runtime_firestore"]["permissions"])
        self.assertIn("OAuth Web client", requirements["browser_authentication"])

    def test_explicit_project_resources_connection_and_cleanup(self):
        calls = []
        environments = []

        def run(command, **options):
            calls.append(command)
            if "--env-vars-file" in command:
                path = Path(command[command.index("--env-vars-file") + 1])
                environments.append(json.loads(path.read_text()))
            output = "https://backprop.example" if "describe" in command else ""
            return subprocess.CompletedProcess(command, 0, stdout=output)

        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "deployment.local.json"
            response = json.dumps({"backend": "gcp", "gcp_id": "a" * 32,
                                   "host": "https://backprop.example",
                                   "client_id": CONFIG["client_id"]}).encode()
            with patch("urllib.request.urlopen", return_value=io.BytesIO(response)):
                result = DEPLOY.deploy(CONFIG, state, run=run)
            self.assertEqual(result["status"], "deployed")
            browser_auth = result["browser_authentication"]
            self.assertEqual(browser_auth["status"], "origin_registration_and_sign_in_required")
            self.assertEqual(browser_auth["authorized_javascript_origin"], result["origin"])
            self.assertEqual(browser_auth["client_id"], CONFIG["client_id"])
            self.assertIn(CONFIG["project"], browser_auth["clients_url"])
            self.assertEqual(len(environments), 2)
            self.assertTrue(all(environment["BACKPROP_GOOGLE_CLIENT_ID"] == CONFIG["client_id"]
                                for environment in environments))
            self.assertEqual(set(result["owned"]), {
                "bucket", "account", "repository", "database", "firestore_access", "service",
            })
            self.assertEqual(json.loads(Path(result["connection_path"]).read_text())["backend"], "gcp")
            self.assertTrue(all(command[-3:-1] == ["--project", CONFIG["project"]] for command in calls))
            self.assertTrue(any("--no-invoker-iam-check" in command for command in calls))
            deployment = next(command for command in calls if command[1:3] == ["run", "deploy"])
            self.assertEqual(deployment[deployment.index("--concurrency") + 1], "16")
            self.assertEqual(deployment[deployment.index("--min-instances") + 1], "0")
            self.assertFalse(any(command[1:3] == ["services", "enable"] for command in calls))
            bindings = [command for command in calls if command[1] == "projects"]
            self.assertEqual(len(bindings), 1)
            self.assertIn("roles/datastore.user", bindings[0])
            condition = bindings[0][bindings[0].index("--condition") + 1]
            self.assertIn('resource.name=="projects/customer-project/databases/backprop-test"', condition)
            self.assertTrue(all(environment["BACKPROP_FIRESTORE_DATABASE"] == CONFIG["name"]
                                for environment in environments))
            self.assertTrue(all(environment["BACKPROP_GCP_PROJECT"] == CONFIG["project"]
                                for environment in environments))
            self.assertTrue(any("--disable-indexes" in command for command in calls))
            result = DEPLOY.cleanup(state, run=run)
            self.assertEqual(result["status"], "deleted")
            self.assertEqual(result["owned"], [])
            removals = [command for command in calls if "remove-iam-policy-binding" in command]
            self.assertEqual(len(removals), 1)
            self.assertEqual(removals[0][removals[0].index("--condition") + 1], condition)
            deletion_count = len(calls)
            DEPLOY.cleanup(state, run=run)
            self.assertEqual(len(calls), deletion_count)

    def test_missing_or_invalid_oauth_config_creates_no_resources_or_ledger(self):
        """
        Purpose: Refuse incomplete browser setup before any cloud change.
        Input: Missing, malformed, and contradictory OAuth settings.
        Output: A configuration error without a ledger or cloud call.
        """
        invalid_configs = [
            {**CONFIG, "client_id": value}
            for value in (None, "", "not-a-google-client", "client.apps.googleusercontent.com ")
        ]
        invalid_configs.extend([
            {key: value for key, value in CONFIG.items() if key != "client_id"},
            {**CONFIG, "sdk_only": "true"},
            {**CONFIG, "sdk_only": True},
        ])
        for config in invalid_configs:
            with self.subTest(config=config), tempfile.TemporaryDirectory() as directory:
                state = Path(directory) / "deployment.local.json"
                with patch("subprocess.run") as run, self.assertRaises(ValueError):
                    DEPLOY.deploy(config, state, run=run)
                run.assert_not_called()
                self.assertFalse(state.exists())

    def test_explicit_sdk_only_deployment_and_legacy_cleanup(self):
        """
        Purpose: Preserve intentional SDK access and cleanup of older installations.
        Input: An SDK-only deployment and a ledger without the new selection.
        Output: Explicit SDK status and successful resource cleanup.
        """
        config = {key: value for key, value in CONFIG.items() if key != "client_id"}
        config["sdk_only"] = True

        def run(command, **options):
            output = "https://backprop.example" if "describe" in command else ""
            return subprocess.CompletedProcess(command, 0, stdout=output)

        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "deployment.local.json"
            response = json.dumps({"backend": "gcp", "gcp_id": "a" * 32,
                                   "host": "https://backprop.example"}).encode()
            with patch("urllib.request.urlopen", return_value=io.BytesIO(response)):
                result = DEPLOY.deploy(config, state, run=run)
            self.assertEqual(result["browser_authentication"], {"status": "sdk_only"})
            # Older deployments omitted both client_id and the explicit SDK mode.
            result["config"].pop("sdk_only")
            state.write_text(json.dumps(result))
            self.assertEqual(DEPLOY.cleanup(state, run=run)["status"], "deleted")

    def test_deployed_oauth_mismatch_preserves_resources_for_cleanup(self):
        """
        Purpose: Reject stale runtime OAuth settings without loss of cleanup evidence.
        Input: A public connection with an unexpected client ID.
        Output: A failed deployment and a complete resource ownership ledger.
        """
        def run(command, **options):
            output = "https://backprop.example" if "describe" in command else ""
            return subprocess.CompletedProcess(command, 0, stdout=output)

        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "deployment.local.json"
            response = json.dumps({"client_id": "different.apps.googleusercontent.com"}).encode()
            with patch("urllib.request.urlopen", return_value=io.BytesIO(response)), \
                    self.assertRaisesRegex(RuntimeError, "OAuth client ID"):
                DEPLOY.deploy(CONFIG, state, run=run)
            failed = json.loads(state.read_text())
            self.assertEqual(failed["status"], "failed")
            self.assertIn("service", failed["owned"])
            self.assertEqual(DEPLOY.cleanup(state, run=run)["owned"], [])

    def test_existing_bucket_failure_never_claims_or_deletes_it(self):
        def fail(command, **options):
            raise subprocess.CalledProcessError(1, command)

        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "deployment.local.json"
            with self.assertRaises(subprocess.CalledProcessError):
                DEPLOY.deploy(CONFIG, state, run=fail)
            self.assertEqual(json.loads(state.read_text())["owned"], [])
            self.assertEqual(DEPLOY.cleanup(state, run=fail)["status"], "deleted")

    def test_first_use_firestore_iam_conflict_retries_without_recreating_resources(self):
        calls = []
        conflicted = False

        def run(command, **options):
            nonlocal conflicted
            calls.append(command)
            if "add-iam-policy-binding" in command and command[1] == "projects" and not conflicted:
                conflicted = True
                raise subprocess.CalledProcessError(1, command)
            output = "https://backprop.example" if "describe" in command else ""
            return subprocess.CompletedProcess(command, 0, stdout=output)

        response = json.dumps({"client_id": CONFIG["client_id"]}).encode()
        with tempfile.TemporaryDirectory() as directory, \
                patch("urllib.request.urlopen", return_value=io.BytesIO(response)), \
                patch.object(DEPLOY.time, "sleep"):
            state = Path(directory) / "deployment.local.json"
            result = DEPLOY.deploy(CONFIG, state, run=run)
        self.assertEqual(result["status"], "deployed")
        self.assertEqual(sum(command[1] == "projects" for command in calls), 2)
        self.assertEqual(sum(command[1:4] == ["firestore", "databases", "create"]
                             for command in calls), 1)

    def test_failed_cleanup_preserves_only_failed_resources_for_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "deployment.local.json"
            state.write_text(json.dumps({"config": CONFIG, "bucket": "customer-test",
                                         "account": "runtime@example.com", "owned": ["bucket", "service"]}))

            def fail_bucket(command, **options):
                return subprocess.CompletedProcess(command, 1 if "storage" in command else 0,
                                                   stdout="", stderr="unavailable")

            with self.assertRaisesRegex(RuntimeError, "bucket"):
                DEPLOY.cleanup(state, run=fail_bucket)
            self.assertEqual(json.loads(state.read_text())["owned"], ["bucket"])

    def test_failed_iam_grant_cleanup_confirms_absence_without_removing_other_bindings(self):
        calls = []

        def run(command, **options):
            calls.append(command)
            missing = "remove-iam-policy-binding" in command
            policy = {"bindings": [{"role": "roles/datastore.user", "members": ["serviceAccount:unrelated@example.com"]}]}
            return subprocess.CompletedProcess(command, int(missing),
                                               stdout=json.dumps(policy), stderr="absent" if missing else "")

        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.local.json"
            state.write_text(json.dumps({"config": CONFIG, "account": "owned@example.com",
                                         "bucket": "test-bucket", "owned": ["firestore_access"]}))
            result = DEPLOY.cleanup(state, run=run)
        self.assertEqual(result["status"], "deleted")
        self.assertEqual(len(calls), 2)
        self.assertIn("get-iam-policy", calls[-1])


if __name__ == "__main__":
    unittest.main()
