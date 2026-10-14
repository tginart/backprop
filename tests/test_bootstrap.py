"""Keep organization resource IDs separate from OAuth marking identifiers."""

from contextlib import redirect_stdout
import importlib.util
import io
from types import SimpleNamespace
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "deploy"))
SPEC = importlib.util.spec_from_file_location("backprop_bootstrap", ROOT / "deploy/bootstrap.py")
BOOTSTRAP = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BOOTSTRAP)

MARKING = "11111111-2222-4333-8444-555555555555"
PROJECT = "ri.compass.main.folder.project"
ORG = "ri.multipass..organization.synthetic"
HOST = "https://foundry.example.com"
APPLICATION = "ri.third-party-applications.main.application.test"


class FakeFoundry:
    def __init__(self):
        self.calls = []
        self.host = HOST

    def request(self, path, method="GET", body=None):
        self.calls.append((path, method, body))
        if path.startswith("/api/v2/admin/organizations/"):
            return {"rid": ORG, "markingId": MARKING}
        if path.endswith("/applications/v5"):
            return {"application": {"rid": APPLICATION}}
        if "/application-clients/" in path:
            return {"type": "created", "created": {"clientId": "public-client"}}
        if "/client-installations/" in path:
            return {"installation": None}
        raise AssertionError(path)


class BootstrapTests(unittest.TestCase):
    def test_app_and_installation_use_real_marking_uuid(self):
        api = FakeFoundry()
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            state.write_text(json.dumps({"host": HOST, "project_rid": PROJECT}))
            config = SimpleNamespace(host=HOST, deployment_state_path=state,
                resource_state_path=Path(directory) / "resources.json",
                bootstrap={"space_rid": "ri.compass.main.space.synthetic",
                           "organization_rid": ORG, "owner_user_id": "synthetic-owner"})
            with redirect_stdout(io.StringIO()):
                result = BOOTSTRAP.bootstrap(config, api)
            self.assertEqual(result["client_id"], "public-client")
            self.assertEqual(json.loads(state.read_text())["host"], HOST)
        application = next(body for path, method, body in api.calls if path.endswith("/applications/v5"))
        installation = next(body for path, method, body in api.calls if "/client-installations/" in path and method == "PUT")
        self.assertEqual(application["organizationRid"], ORG)
        self.assertEqual(application["clientAllowedMarkingIdsV2"], [MARKING])
        self.assertEqual(installation["markingIds"], [MARKING])
        self.assertEqual(installation["resources"], [PROJECT])
        self.assertEqual(set(application["authScope"]["platform"]["projects"]), {PROJECT})

    def test_bad_marking_identifier_stops_before_any_mutation(self):
        class WrongIdentifier:
            def request(self, *args):
                return {"markingId": ORG}
        with self.assertRaises(ValueError):
            BOOTSTRAP.organization_marking_id(WrongIdentifier(), ORG)


if __name__ == "__main__":
    unittest.main()
