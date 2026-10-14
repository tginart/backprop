"""Exercise portable operator commands against one isolated installation.

Architecture: temporary config and state -> real bootstrap/domain/help entry
points -> fake Foundry responses -> inspect exact mutations and selected files.
Credential retrieval and network access are never used. A repeated bootstrap
must reuse saved identifiers, and conflicting state must fail before API calls.
Main callers: unittest discovery and portable deployment acceptance checks.
"""

from contextlib import redirect_stderr, redirect_stdout
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "deploy"))
from local_config import load_deployment_config

MODULES = {}
for name in ("bootstrap", "configure_domain", "configure_controls", "deploy"):
    spec = importlib.util.spec_from_file_location("backprop_portable_" + name, ROOT / "deploy" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    MODULES[name] = module
BOOTSTRAP = MODULES["bootstrap"]
DOMAIN = MODULES["configure_domain"]

HOST = "https://customer-foundry.example.com"
PROJECT = "ri.compass.main.folder.customer"
APPLICATION = "ri.third-party-applications.main.application.customer"
CLIENT = "customer-public-client"
MARKING = "11111111-2222-4333-8444-555555555555"
REGISTERED_DOMAIN = "customer-backprop.example.com"
SETTINGS = {"space_rid": "ri.compass.main.space.customer",
            "organization_rid": "ri.multipass..organization.customer",
            "owner_user_id": "customer-owner"}


class BootstrapApi:
    """Return explicit customer fixtures and remember an enabled installation."""

    host = HOST

    def __init__(self):
        self.calls = []
        self.installation = None

    def request(self, path, method="GET", body=None):
        self.calls.append((path, method, copy.deepcopy(body)))
        if path == "/api/v2/admin/organizations/" + SETTINGS["organization_rid"] and method == "GET":
            return {"rid": SETTINGS["organization_rid"], "markingId": MARKING}
        if path == "/api/v2/filesystem/projects/create" and method == "POST":
            return {"rid": PROJECT, "path": "/customer-space/Backprop"}
        if path == "/third-party-application-service/api/applications/v5" and method == "POST":
            return {"application": {"rid": APPLICATION}}
        if path == "/third-party-application-service/api/application-clients/" + APPLICATION and method == "GET":
            return {"type": "notRequested"}
        if path == "/third-party-application-service/api/application-clients/v2/" + APPLICATION and method == "PUT":
            return {"type": "created", "created": {"clientId": CLIENT}}
        if path.startswith("/multipass/api/client-installations/" + CLIENT + "?"):
            if method == "GET":
                return {"installation": copy.deepcopy(self.installation)}
            if method == "PUT":
                self.installation = copy.deepcopy(body)
                return None
        raise AssertionError(f"Unexpected API call: {method} {path}")


class DomainApi:
    """Permit only website lookup and the callback change under test."""

    host = HOST

    def __init__(self, subdomains=None):
        self.calls = []
        self.subdomains = [REGISTERED_DOMAIN] if subdomains is None else subdomains

    def request(self, path, method="GET", body=None):
        self.calls.append((path, method, copy.deepcopy(body)))
        if path == "/api/v2/thirdPartyApplications/" + APPLICATION + "/website" and method == "GET":
            return {"subdomains": self.subdomains}
        if path == "/third-party-application-service/api/applications/" + APPLICATION + "/client-config" and method == "PUT":
            return None
        raise AssertionError(f"Unexpected API call: {method} {path}")


class OperatorFlowFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name).resolve()
        self.config_path = self.directory / "selected" / "config.json"
        self.config_path.parent.mkdir()
        self.config_path.write_text(json.dumps({"host": HOST,
                                               "deployment_state": "application.json",
                                               "resource_state": "resources.json",
                                               "bootstrap": SETTINGS}))
        self.config = load_deployment_config(self.config_path)

    def tearDown(self):
        self.temporary.cleanup()

    def write_state(self, path, state):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, indent=2) + "\n")

    def completed_states(self):
        state = {"host": HOST, "project_rid": PROJECT, "application_rid": APPLICATION,
                 "client_id": CLIENT, "organization_rid": SETTINGS["organization_rid"],
                 "app_url": "https://previous-backprop.example.com/"}
        resources = {"host": HOST, "project_rid": PROJECT, "branch": "master",
                     "stream_dataset_rid": "ri.foundry.main.dataset.events",
                     "media_dataset_rid": "ri.foundry.main.dataset.media",
                     "app_url": state["app_url"]}
        return state, resources


class PortableBootstrapTests(OperatorFlowFixture):
    def test_selected_space_organization_and_owner_drive_creation_and_reuse(self):
        api = BootstrapApi()
        config_before = self.config_path.read_bytes()
        with redirect_stdout(io.StringIO()), \
             patch.object(BOOTSTRAP, "FoundryApi", side_effect=AssertionError("Bootstrap requested a credential")):
            state = BOOTSTRAP.bootstrap(self.config, api)
        self.assertEqual(state["host"], HOST)
        self.assertEqual(state["organization_rid"], SETTINGS["organization_rid"])
        self.assertEqual(state["project_rid"], PROJECT)
        self.assertEqual(state["application_rid"], APPLICATION)
        self.assertEqual(state["client_id"], CLIENT)
        self.assertEqual(json.loads(self.config.deployment_state_path.read_text()), state)
        self.assertFalse(self.config.resource_state_path.exists())
        self.assertEqual(self.config_path.read_bytes(), config_before)
        project = next(body for path, method, body in api.calls if path.endswith("/projects/create"))
        self.assertEqual(project["spaceRid"], SETTINGS["space_rid"])
        self.assertEqual(project["organizationRids"], [SETTINGS["organization_rid"]])
        self.assertEqual(project["roleGrants"], {"compass:manage": [
            {"principalId": SETTINGS["owner_user_id"], "principalType": "USER"}]})
        application = next(body for path, method, body in api.calls if path.endswith("/applications/v5"))
        self.assertEqual(application["organizationRid"], SETTINGS["organization_rid"])
        self.assertEqual(application["parentFolderRid"], PROJECT)
        self.assertEqual(application["clientAllowedMarkingIdsV2"], [MARKING])
        installation_path, _, installation = next(call for call in api.calls if "/client-installations/" in call[0] and call[1] == "PUT")
        self.assertEqual(parse_qs(urlsplit(installation_path).query), {"organizationRid": [SETTINGS["organization_rid"]]})
        self.assertEqual(installation["resources"], [PROJECT])
        self.assertEqual(installation["markingIds"], [MARKING])
        api.calls.clear()
        with redirect_stdout(io.StringIO()):
            repeated = BOOTSTRAP.bootstrap(self.config, api)
        self.assertEqual(repeated, state)
        self.assertTrue(api.calls)
        self.assertTrue(all(method == "GET" for path, method, body in api.calls))
        self.assertEqual(self.config_path.read_bytes(), config_before)

    def test_mismatched_saved_bindings_fail_before_credentials_network_or_writes(self):
        state, resources = self.completed_states()
        changes = [("deployment", "host", "https://different-foundry.example.com"),
                   ("resource", "host", "https://different-foundry.example.com"),
                   ("deployment", "organization_rid", "ri.multipass..organization.different"),
                   ("resource", "project_rid", "ri.compass.main.folder.different"),
                   ("resource", "app_url", "https://different-app.example.com/")]
        for location, key, value in changes:
            selected_state = {**state, **({key: value} if location == "deployment" else {})}
            selected_resources = {**resources, **({key: value} if location == "resource" else {})}
            self.write_state(self.config.deployment_state_path, selected_state)
            self.write_state(self.config.resource_state_path, selected_resources)
            before = {path: path.read_bytes() for path in (self.config.deployment_state_path, self.config.resource_state_path)}
            api = BootstrapApi()
            with self.subTest(location=location, key=key), redirect_stdout(io.StringIO()), \
                 patch.object(BOOTSTRAP, "FoundryApi", side_effect=AssertionError("Retrieved credentials")):
                with self.assertRaises(ValueError):
                    BOOTSTRAP.bootstrap(self.config)
                with self.assertRaises(ValueError):
                    BOOTSTRAP.bootstrap(self.config, api)
            self.assertEqual(api.calls, [])
            self.assertTrue(all(path.read_bytes() == content for path, content in before.items()))

    def test_wrong_api_host_fails_before_network_or_state_creation(self):
        api = BootstrapApi()
        api.host = "https://different-foundry.example.com"
        with self.assertRaisesRegex(ValueError, "host"):
            BOOTSTRAP.bootstrap(self.config, api)
        self.assertEqual(api.calls, [])
        self.assertFalse(self.config.deployment_state_path.exists())
        self.assertFalse(self.config.resource_state_path.exists())

    def test_existing_resource_project_requires_explicit_deployment_project_before_bootstrap(self):
        """Missing creation progress must never authorize a duplicate project.

        Main callers: bootstrap acceptance checks. If resources already select
        a project, the operator must record that same project before resuming.
        """
        _, resources = self.completed_states()
        self.write_state(self.config.resource_state_path, resources)
        resource_before = self.config.resource_state_path.read_bytes()
        for deployment in (None, {"host": HOST}):
            self.config.deployment_state_path.unlink(missing_ok=True)
            if deployment is not None:
                self.write_state(self.config.deployment_state_path, deployment)
            before = self.config.deployment_state_path.read_bytes() if deployment is not None else None
            api = BootstrapApi()
            with self.subTest(deployment=deployment), \
                 patch.object(BOOTSTRAP, "FoundryApi", side_effect=AssertionError("Retrieved credentials")):
                with self.assertRaisesRegex(ValueError, "project_rid"):
                    BOOTSTRAP.bootstrap(self.config)
                with self.assertRaisesRegex(ValueError, "project_rid"):
                    BOOTSTRAP.bootstrap(self.config, api)
            self.assertEqual(api.calls, [])
            self.assertEqual(self.config.resource_state_path.read_bytes(), resource_before)
            if before is None:
                self.assertFalse(self.config.deployment_state_path.exists())
            else:
                self.assertEqual(self.config.deployment_state_path.read_bytes(), before)

    def test_manual_setup_without_bootstrap_settings_never_requests_credentials(self):
        values = json.loads(self.config_path.read_text())
        del values["bootstrap"]
        self.config_path.write_text(json.dumps(values))
        config = load_deployment_config(self.config_path)
        with patch.object(BOOTSTRAP, "FoundryApi", side_effect=AssertionError("Retrieved credentials")):
            with self.assertRaisesRegex(ValueError, "manual Foundry setup"):
                BOOTSTRAP.bootstrap(config)


class PortableDomainTests(OperatorFlowFixture):
    def run_domain(self, api, app_url="https://" + REGISTERED_DOMAIN + "/"):
        """Run the real CLI against explicit fixture paths and a fake API.

        Main callers: domain acceptance tests. An environment-selected decoy
        proves that the CLI's --config argument controls which files are updated.
        """
        with patch.dict(os.environ, {"BACKPROP_DEPLOY_CONFIG": str(self.directory / "decoy" / "config.json")}, clear=True), \
             patch.object(sys, "argv", ["configure_domain.py", "--config", str(self.config_path), "--app-url", app_url]), \
             patch.object(DOMAIN, "FoundryApi", return_value=api) as factory, \
             redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            DOMAIN.main()
        return factory

    def test_registered_domain_changes_only_selected_state_files_and_callback(self):
        state, resources = self.completed_states()
        self.write_state(self.config.deployment_state_path, state)
        self.write_state(self.config.resource_state_path, resources)
        for name in ("config.json", "application.json", "resources.json"):
            self.write_state(self.directory / "decoy" / name, {"untouched": name})
        before = {path: path.read_bytes() for path in self.directory.rglob("*.json")}
        api = DomainApi()
        factory = self.run_domain(api)
        self.assertEqual(factory.call_count, 1)
        self.assertEqual(factory.call_args.args[0].path, self.config_path)
        self.assertEqual(factory.call_args.args[0].host, HOST)
        app_url = "https://" + REGISTERED_DOMAIN + "/"
        self.assertEqual(json.loads(self.config.deployment_state_path.read_text()),
                         {**state, "app_url": app_url, "subdomains": [REGISTERED_DOMAIN], "domain_approved": True})
        self.assertEqual(json.loads(self.config.resource_state_path.read_text()), {**resources, "app_url": app_url})
        changed = {path for path, content in before.items() if path.read_bytes() != content}
        self.assertEqual(changed, {self.config.deployment_state_path, self.config.resource_state_path})
        self.assertEqual(len(api.calls), 2)
        self.assertEqual(api.calls[1][1], "PUT")
        self.assertEqual(api.calls[1][2], {"clientSpecification": {"type": "public", "public": {
            "authorizationCodeGrant": {"enabled": True, "redirectUrls": [app_url]},
            "refreshTokenGrant": {"enabled": True}}}})

    def test_unregistered_domain_fails_before_callback_mutation_or_local_writes(self):
        state, resources = self.completed_states()
        self.write_state(self.config.deployment_state_path, state)
        self.write_state(self.config.resource_state_path, resources)
        before = {path: path.read_bytes() for path in self.directory.rglob("*.json")}
        api = DomainApi()
        with self.assertRaisesRegex(ValueError, "not registered"):
            self.run_domain(api, "https://unregistered.example.com/")
        self.assertEqual(len(api.calls), 1)
        self.assertEqual(api.calls[0][1], "GET")
        self.assertTrue(all(path.read_bytes() == content for path, content in before.items()))

    def test_mixed_installation_state_fails_before_api_client_creation(self):
        state, resources = self.completed_states()
        changes = (("resource", "host", "https://different-foundry.example.com"),
                   ("resource", "project_rid", "ri.compass.main.folder.different"),
                   ("deployment", "organization_rid", "ri.multipass..organization.different"))
        for location, key, value in changes:
            self.write_state(self.config.deployment_state_path, {**state, **({key: value} if location == "deployment" else {})})
            self.write_state(self.config.resource_state_path, {**resources, **({key: value} if location == "resource" else {})})
            with self.subTest(location=location, key=key), \
                 patch.object(sys, "argv", ["configure_domain.py", "--config", str(self.config_path),
                                            "--app-url", "https://" + REGISTERED_DOMAIN + "/"]), \
                 patch.object(DOMAIN, "FoundryApi", side_effect=AssertionError("Retrieved credentials")):
                with self.assertRaises(ValueError):
                    DOMAIN.main()

    def test_non_root_or_unsafe_domain_url_fails_before_loading_configuration(self):
        urls = ("http://customer-backprop.example.com/", "https://user:password@customer-backprop.example.com/",
                "https://customer-backprop.example.com/path", "https://customer-backprop.example.com/?token=x",
                "https://customer-backprop.example.com:443/", "https://customer-backprop.example.com/#fragment")
        for app_url in urls:
            with self.subTest(app_url=app_url), \
                 patch.object(sys, "argv", ["configure_domain.py", "--app-url", app_url]), \
                 patch.object(DOMAIN, "load_deployment_config", side_effect=AssertionError("Read operator configuration")), \
                 patch.object(DOMAIN, "FoundryApi", side_effect=AssertionError("Retrieved credentials")), \
                 redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as stopped:
                    DOMAIN.main()
            self.assertEqual(stopped.exception.code, 2)


class OperatorHelpTests(unittest.TestCase):
    def test_operator_help_needs_no_local_configuration_state_or_credentials(self):
        for name, module in MODULES.items():
            output = io.StringIO()
            with self.subTest(command=name), \
                 patch.dict(os.environ, {"BACKPROP_DEPLOY_CONFIG": "/nonexistent/operator-config.json"}, clear=True), \
                 patch.object(sys, "argv", [name + ".py", "--help"]), \
                 patch.object(module, "load_deployment_config", side_effect=AssertionError("Read operator config")), \
                 patch.object(module, "FoundryApi", side_effect=AssertionError("Retrieved credentials")), \
                 patch.object(Path, "read_text", side_effect=AssertionError("Read a local file")), \
                 redirect_stdout(output), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as stopped:
                    module.main()
            self.assertEqual(stopped.exception.code, 0)
            self.assertIn("usage:", output.getvalue())


if __name__ == "__main__":
    unittest.main()
