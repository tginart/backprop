"""Keep portable Foundry provisioning bound to one selected host and project.

Architecture: temporary resource state -> real provision -> recorded API calls
and persisted JSON. Every response is a fixture; no resources are created.
Main callers: unittest discovery and deployment readiness checks.
"""

import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "deploy"))
SPEC = importlib.util.spec_from_file_location("backprop_backend_provision", ROOT / "backend/foundry/provision.py")
PROVISION = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PROVISION)

HOST = "https://customer-foundry.example.com"
PROJECT = "ri.compass.main.folder.customer"
STREAM = "ri.foundry.main.dataset.events"
MEDIA = "ri.foundry.main.dataset.media"
VIEW = "ri.foundry-streaming.main.view.events"


class ProvisionApi:
    """Record mutations and provide only the expected native storage responses."""

    host = HOST

    def __init__(self, state_path, fail_media=False):
        self.state_path = state_path
        self.fail_media = fail_media
        self.calls = []
        self.state_before_media = None

    def request(self, path, method="GET", body=None):
        self.calls.append((path, method, body))
        if path == "/api/v2/streams/datasets/create?preview=true" and method == "POST":
            return {"rid": STREAM}
        if path == "/api/v2/datasets" and method == "POST":
            self.state_before_media = json.loads(self.state_path.read_text())
            if self.fail_media:
                raise RuntimeError("Fixture media creation failed")
            return {"rid": MEDIA}
        if path == f"/api/v2/streams/datasets/{STREAM}/streams/master" and method == "GET":
            return {"viewRid": VIEW}
        raise AssertionError(f"Unexpected API call: {method} {path}")


class BackendProvisionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "operator" / "resources.json"

    def tearDown(self):
        self.temporary.cleanup()

    def write_state(self, state):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(state, indent=2) + "\n")

    def test_new_resources_record_the_api_host_and_selected_project(self):
        api = ProvisionApi(self.path)
        state = PROVISION.provision(api, PROJECT, self.path)
        self.assertEqual(state, {"host": HOST, "project_rid": PROJECT, "branch": "master",
                                 "stream_dataset_rid": STREAM, "media_dataset_rid": MEDIA,
                                 "stream_view_rid": VIEW})
        self.assertEqual(json.loads(self.path.read_text()), state)
        mutations = [(path, body) for path, method, body in api.calls if method == "POST"]
        self.assertEqual(len(mutations), 2)
        self.assertTrue(all(body["parentFolderRid"] == PROJECT for path, body in mutations))
        self.assertEqual(mutations[0][1]["branchName"], "master")
        self.assertEqual(api.state_before_media["host"], HOST)
        self.assertEqual(api.state_before_media["stream_dataset_rid"], STREAM)
        self.assertNotIn("media_dataset_rid", api.state_before_media)

    def test_partial_failure_preserves_the_stream_for_a_safe_retry(self):
        failing = ProvisionApi(self.path, fail_media=True)
        with self.assertRaisesRegex(RuntimeError, "media creation failed"):
            PROVISION.provision(failing, PROJECT, self.path)
        persisted = json.loads(self.path.read_text())
        self.assertEqual(persisted["stream_dataset_rid"], STREAM)
        self.assertEqual(persisted["host"], HOST)
        self.assertNotIn("media_dataset_rid", persisted)
        retry = ProvisionApi(self.path)
        result = PROVISION.provision(retry, PROJECT, self.path)
        self.assertEqual(result["media_dataset_rid"], MEDIA)
        self.assertFalse(any("/datasets/create" in path for path, method, body in retry.calls))

    def test_existing_resources_are_reused_without_creating_datasets(self):
        state = {"host": HOST, "project_rid": PROJECT, "branch": "master",
                 "stream_dataset_rid": STREAM, "media_dataset_rid": MEDIA}
        self.write_state(state)
        api = ProvisionApi(self.path)
        self.assertEqual(PROVISION.provision(api, PROJECT, self.path), {**state, "stream_view_rid": VIEW})
        self.assertEqual(api.calls, [(f"/api/v2/streams/datasets/{STREAM}/streams/master", "GET", None)])

    def test_mismatched_host_project_or_branch_stops_before_any_api_call_or_write(self):
        valid = {"host": HOST, "project_rid": PROJECT, "branch": "master",
                 "stream_dataset_rid": STREAM}
        for key, value in (("host", "https://different-foundry.example.com"),
                           ("project_rid", "ri.compass.main.folder.different"),
                           ("branch", "development")):
            self.write_state({**valid, key: value})
            before = self.path.read_bytes()
            api = ProvisionApi(self.path)
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "project" if key == "project_rid" else key):
                PROVISION.provision(api, PROJECT, self.path)
            self.assertEqual(api.calls, [])
            self.assertEqual(self.path.read_bytes(), before)

    def test_placeholder_project_stops_before_resource_creation(self):
        for project in ("YOUR_PROJECT_RID", "REPLACE_ME", "<project-rid>"):
            api = ProvisionApi(self.path)
            with self.subTest(project=project), self.assertRaises(ValueError):
                PROVISION.provision(api, project, self.path)
            self.assertEqual(api.calls, [])
            self.assertFalse(self.path.exists())

    def test_cli_conflicting_deployment_project_fails_before_auth_with_no_resource_progress(self):
        """A new resource file must not permit provisioning another app's project.

        Main callers: backend operator acceptance checks. The CLI checks the
        selected application state before constructing an authenticated client,
        even when resource provisioning has never started or saved no progress.
        """
        self.path.parent.mkdir()
        config = self.path.parent / "config.json"
        config.write_text(json.dumps({"host": HOST, "deployment_state": "application.json",
                                      "resource_state": self.path.name}))
        deployment = self.path.parent / "application.json"
        deployment.write_text(json.dumps({"host": HOST, "project_rid": PROJECT}))
        other_project = "ri.compass.main.folder.different"
        for resource_state in (None, {}):
            self.path.unlink(missing_ok=True)
            if resource_state is not None:
                self.write_state(resource_state)
            before = {path: path.read_bytes() for path in self.path.parent.glob("*.json")}
            with self.subTest(resource_state=resource_state), \
                 patch.object(sys, "argv", ["provision.py", "--config", str(config), "--project-rid", other_project]), \
                 patch.object(PROVISION, "FoundryApi", side_effect=AssertionError("Retrieved credentials")) as factory:
                with self.assertRaisesRegex(ValueError, "project"):
                    PROVISION.main()
            factory.assert_not_called()
            self.assertEqual({path: path.read_bytes() for path in self.path.parent.glob("*.json")}, before)

    def test_cli_placeholder_project_fails_before_auth_without_any_saved_state(self):
        self.path.parent.mkdir()
        config = self.path.parent / "config.json"
        config.write_text(json.dumps({"host": HOST, "deployment_state": "application.json",
                                      "resource_state": self.path.name}))
        before = config.read_bytes()
        for project in ("YOUR_PROJECT_RID", "REPLACE_ME", "<project-rid>"):
            with self.subTest(project=project), \
                 patch.object(sys, "argv", ["provision.py", "--config", str(config), "--project-rid", project]), \
                 patch.object(PROVISION, "FoundryApi", side_effect=AssertionError("Retrieved credentials")) as factory:
                with self.assertRaisesRegex(ValueError, "project_rid"):
                    PROVISION.main()
            factory.assert_not_called()
            self.assertEqual(config.read_bytes(), before)
            self.assertFalse(self.path.exists())
            self.assertFalse((self.path.parent / "application.json").exists())


if __name__ == "__main__":
    unittest.main()
