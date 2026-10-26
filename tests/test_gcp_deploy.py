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
          "allowed_emails": ["operator@example.com"], "prefix": "isolated"}


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
        self.assertNotIn("resourcemanager.projects.setIamPolicy", permissions)
        self.assertIn("OAuth Web client", requirements["browser_authentication"])

    def test_explicit_project_resources_connection_and_cleanup(self):
        calls = []

        def run(command, **options):
            calls.append(command)
            output = "https://backprop.example" if "describe" in command else ""
            return subprocess.CompletedProcess(command, 0, stdout=output)

        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "deployment.local.json"
            response = json.dumps({"backend": "gcp", "gcp_id": "a" * 32,
                                   "host": "https://backprop.example"}).encode()
            with patch("urllib.request.urlopen", return_value=io.BytesIO(response)):
                result = DEPLOY.deploy(CONFIG, state, run=run)
            self.assertEqual(result["status"], "ready")
            self.assertEqual(set(result["owned"]), {"bucket", "account", "repository", "service"})
            self.assertEqual(json.loads(Path(result["connection_path"]).read_text())["backend"], "gcp")
            self.assertTrue(all(command[-3:-1] == ["--project", CONFIG["project"]] for command in calls))
            self.assertTrue(any("--no-invoker-iam-check" in command for command in calls))
            self.assertFalse(any(command[1:3] == ["services", "enable"] for command in calls))
            self.assertFalse(any(command[1] == "projects" for command in calls))
            result = DEPLOY.cleanup(state, run=run)
            self.assertEqual(result["status"], "deleted")
            self.assertEqual(result["owned"], [])
            deletion_count = len(calls)
            DEPLOY.cleanup(state, run=run)
            self.assertEqual(len(calls), deletion_count)

    def test_existing_bucket_failure_never_claims_or_deletes_it(self):
        def fail(command, **options):
            raise subprocess.CalledProcessError(1, command)

        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "deployment.local.json"
            with self.assertRaises(subprocess.CalledProcessError):
                DEPLOY.deploy(CONFIG, state, run=fail)
            self.assertEqual(json.loads(state.read_text())["owned"], [])
            self.assertEqual(DEPLOY.cleanup(state, run=fail)["status"], "deleted")

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


if __name__ == "__main__":
    unittest.main()
