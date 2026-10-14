"""Verify a fresh checkout can select a deployment without local credentials.

Architecture: temporary operator JSON -> real loader -> configured API with
mocked token sources. State and tokens stay inside each test fixture; no test
reads real operator files or contacts Foundry, gcloud, or Secret Manager.
Main callers: unittest discovery and portable deployment readiness checks.
"""

import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from urllib.request import Request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "deploy"))
import foundry_api
import local_config

HOST = "https://customer-foundry.example.com"
PROJECT = "ri.compass.main.folder.customer"
SECRET_MANAGER = {"project": "customer-cloud-project", "secret": "customer-foundry-token"}
BOOTSTRAP = {"space_rid": "ri.compass.main.space.customer",
             "organization_rid": "ri.multipass..organization.customer",
             "owner_user_id": "customer-owner"}


class OperatorConfigFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.path = self.directory / "operator" / "config.json"
        self.path.parent.mkdir()
        self.values = {"host": HOST, "deployment_state": "state/application.json",
                       "resource_state": "../resources/state.json"}

    def tearDown(self):
        self.temporary.cleanup()

    def write_config(self, values=None, path=None):
        selected = path or self.path
        selected.parent.mkdir(parents=True, exist_ok=True)
        selected.write_text(json.dumps(self.values if values is None else values))
        return selected

    def load(self, values=None):
        return local_config.load_deployment_config(self.write_config(values))


class DeploymentConfigTests(OperatorConfigFixture):
    def test_paths_are_resolved_relative_to_the_selected_config_file(self):
        config = self.load()
        self.assertIsInstance(config, local_config.DeploymentConfig)
        self.assertEqual(config.host, HOST)
        self.assertEqual(config.path, self.path.resolve())
        self.assertEqual(config.deployment_state_path, (self.path.parent / "state/application.json").resolve())
        self.assertEqual(config.resource_state_path, (self.path.parent / "../resources/state.json").resolve())
        self.assertIsNone(config.secret_manager)
        self.assertIsNone(config.bootstrap)

    def test_equivalent_https_hosts_use_one_origin_without_rewriting_saved_files(self):
        config = self.load({**self.values, "host": "https://CUSTOMER-FOUNDRY.Example.com:443"})
        self.assertEqual(config.host, HOST)
        resources = {"host": "https://Customer-Foundry.example.com:443", "project_rid": PROJECT,
                     "branch": "master", "stream_dataset_rid": "ri.foundry.main.dataset.events",
                     "media_dataset_rid": "ri.foundry.main.dataset.media"}
        state = {"host": "https://CUSTOMER-FOUNDRY.example.com", "project_rid": PROJECT,
                 "application_rid": "ri.third-party-applications.main.application.customer",
                 "client_id": "public-client"}
        for path, values in ((config.resource_state_path, resources), (config.deployment_state_path, state)):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(values))
        before = {path: path.read_bytes() for path in (config.resource_state_path, config.deployment_state_path)}
        loaded_resources = local_config.read_resource_state(config)
        self.assertEqual(loaded_resources, {**resources, "host": HOST})
        self.assertEqual(local_config.read_deployment_state(config, loaded_resources), {**state, "host": HOST})
        api = foundry_api.FoundryApi(config, token="fixture-token")
        self.assertEqual(api.host, HOST)
        self.assertTrue(all(path.read_bytes() == content for path, content in before.items()))

    def test_environment_override_and_explicit_selection_are_independent_of_cwd(self):
        self.write_config()
        alternate = self.directory / "other" / "config.json"
        self.write_config({**self.values, "host": "https://other-foundry.example.com"}, alternate)
        with patch.dict(os.environ, {"BACKPROP_DEPLOY_CONFIG": str(self.path)}, clear=True):
            self.assertEqual(local_config.load_deployment_config().path, self.path.resolve())
            self.assertEqual(local_config.load_deployment_config(alternate).host, "https://other-foundry.example.com")

    def test_default_config_is_an_operator_file_under_deploy(self):
        with patch.dict(os.environ, {}, clear=True), \
             patch.object(Path, "read_text", return_value=json.dumps(self.values)) as read:
            config = local_config.load_deployment_config()
        self.assertEqual(config.path, ROOT / "deploy" / "config.local.json")
        self.assertEqual(read.call_count, 1)

    def test_import_does_not_read_operator_configuration(self):
        with patch.dict(os.environ, {"BACKPROP_DEPLOY_CONFIG": str(self.directory / "missing.json")}, clear=True), \
             patch.object(Path, "read_text", side_effect=AssertionError("Import read operator state")):
            importlib.reload(local_config)
            importlib.reload(foundry_api)

    def test_optional_credentials_and_bootstrap_fields_load_without_side_effects(self):
        with patch("foundry_api.subprocess.run", side_effect=AssertionError("Loader requested a credential")):
            config = self.load({**self.values, "secret_manager": SECRET_MANAGER, "bootstrap": BOOTSTRAP})
        self.assertIsNotNone(config.secret_manager)
        self.assertIsNotNone(config.bootstrap)

    def test_unknown_fields_are_rejected_at_every_config_level(self):
        invalid = [{**self.values, "typo_host": HOST},
                   {**self.values, "secret_manager": {**SECRET_MANAGER, "token": "must-not-read"}},
                   {**self.values, "bootstrap": {**BOOTSTRAP, "organization": "typo"}}]
        for values in invalid:
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.load(values)

    def test_required_fields_and_object_shapes_are_validated(self):
        invalid = [{key: value for key, value in self.values.items() if key != missing}
                   for missing in self.values]
        invalid += [[], "configuration", {**self.values, "deployment_state": 1},
                    {**self.values, "resource_state": ""}, {**self.values, "secret_manager": "project"},
                    {**self.values, "bootstrap": []}, {**self.values, "secret_manager": {"project": "cloud"}},
                    {**self.values, "bootstrap": {"space_rid": BOOTSTRAP["space_rid"]}}]
        for values in invalid:
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.load(values)

    def test_foundry_host_must_be_a_safe_https_origin(self):
        invalid = ["http://foundry.example.com", "https://user:password@foundry.example.com",
                   "https://foundry.example.com/path", "https://foundry.example.com?token=x",
                   "https://foundry.example.com#fragment", "https://foundry.example.com\n",
                   "https://foundry.example.com; connect-src *", "https://*.example.com",
                   "https://foundry.example.com:65536", "https://foundry.example.com:0"]
        for host in invalid:
            with self.subTest(host=host), self.assertRaises(ValueError):
                self.load({**self.values, "host": host})

    def test_placeholders_in_operator_identifiers_are_rejected(self):
        invalid = [{**self.values, "secret_manager": {**SECRET_MANAGER, "project": "YOUR_PROJECT_ID"}},
                   {**self.values, "secret_manager": {**SECRET_MANAGER, "secret": "REPLACE_ME"}},
                   {**self.values, "bootstrap": {**BOOTSTRAP, "owner_user_id": "<owner-user-id>"}}]
        for values in invalid:
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.load(values)

    def test_resource_state_must_match_the_selected_foundry_host(self):
        config = self.load()
        config.resource_state_path.parent.mkdir(parents=True)
        for host in (HOST, "https://different-foundry.example.com"):
            state = {"host": host, "project_rid": PROJECT, "branch": "master",
                     "stream_dataset_rid": "ri.foundry.main.dataset.events",
                     "media_dataset_rid": "ri.foundry.main.dataset.media"}
            config.resource_state_path.write_text(json.dumps(state))
            if host == HOST:
                self.assertEqual(local_config.read_resource_state(config), state)
            else:
                with self.assertRaisesRegex(ValueError, "host"):
                    local_config.read_resource_state(config)

    def test_deployment_state_rejects_an_unrelated_host_or_project(self):
        config = self.load()
        config.deployment_state_path.parent.mkdir(parents=True)
        resources = {"host": HOST, "project_rid": PROJECT, "branch": "master"}
        valid = {"host": HOST, "project_rid": PROJECT, "client_id": "public-client",
                 "application_rid": "ri.third-party-applications.main.application.customer",
                 "app_url": "https://backprop.example.com/"}
        config.deployment_state_path.write_text(json.dumps(valid))
        self.assertEqual(local_config.read_deployment_state(config, resources), valid)
        for key, value in (("host", "https://different-foundry.example.com"),
                           ("project_rid", "ri.compass.main.folder.different")):
            config.deployment_state_path.write_text(json.dumps({**valid, key: value}))
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, key):
                local_config.read_deployment_state(config, resources)

    def test_operator_files_are_ignored_and_examples_remain_publishable(self):
        # Exercise the actual ignore rules without depending on private Git history.
        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory)
            (fixture / ".gitignore").write_bytes((ROOT / ".gitignore").read_bytes())
            subprocess.run(["git", "init", "--quiet", str(fixture)], check=True, capture_output=True)
            for relative, expected in (("deploy/config.local.json",0), ("deploy/state.json",0),
                    ("backend/state.json",0), ("deploy/config.example.json",1),
                    ("deploy/state.example.json",1), ("backend/state.example.json",1)):
                with self.subTest(path=relative):
                    if expected == 1:self.assertTrue((ROOT / relative).is_file())
                    result = subprocess.run(["git", "check-ignore", "--no-index", "--quiet", relative],
                                            cwd=fixture, capture_output=True, text=True, check=False)
                    self.assertEqual(result.returncode, expected, result.stderr)



class FoundryCredentialTests(OperatorConfigFixture):
    """Test credential selection without importing any real operator token.

    Main callers: deployment readiness checks. An explicitly selected empty or
    malformed credential must fail instead of silently choosing another source.
    """

    def make_config(self, secret_manager=True):
        values = {**self.values, "secret_manager": SECRET_MANAGER} if secret_manager else self.values
        return self.load(values)

    def test_explicit_token_uses_the_configured_host_and_skips_other_sources(self):
        config = self.make_config()
        with patch.dict(os.environ, {"BACKPROP_TOKEN": "env-token", "FOUNDRY_TOKEN": "fallback-token"}, clear=True), \
             patch("foundry_api.subprocess.run", side_effect=AssertionError("Requested Secret Manager")):
            api = foundry_api.FoundryApi(config, token="explicit-token")
        self.assertIs(api.config, config)
        self.assertEqual(api.host, HOST)
        self.assertEqual(api.token, "explicit-token")

    def test_environment_precedence_skips_secret_manager(self):
        for environment, expected in (({"BACKPROP_TOKEN": "backprop-token", "FOUNDRY_TOKEN": "foundry-token"}, "backprop-token"),
                                      ({"FOUNDRY_TOKEN": "foundry-token"}, "foundry-token")):
            with self.subTest(environment=environment), patch.dict(os.environ, environment, clear=True), \
                 patch("foundry_api.subprocess.run", side_effect=AssertionError("Requested Secret Manager")):
                self.assertEqual(foundry_api.FoundryApi(self.make_config()).token, expected)

    def test_configured_secret_manager_uses_selected_project_secret_and_version(self):
        for version in (None, "7"):
            secret = {**SECRET_MANAGER, **({"version": version} if version else {})}
            config = self.load({**self.values, "secret_manager": secret})
            with self.subTest(version=version), patch.dict(os.environ, {}, clear=True), \
                 patch("foundry_api.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "manager-token\n", "")) as run:
                self.assertEqual(foundry_api.FoundryApi(config).token, "manager-token")
            self.assertEqual(run.call_args.args[0], ["gcloud", "secrets", "versions", "access", version or "latest",
                                                    "--secret=" + SECRET_MANAGER["secret"],
                                                    "--project=" + SECRET_MANAGER["project"]])
            self.assertTrue(run.call_args.kwargs["check"])
            self.assertTrue(run.call_args.kwargs["capture_output"])

    def test_omitted_api_config_loads_the_selected_operator_file(self):
        self.write_config()
        with patch.dict(os.environ, {"BACKPROP_DEPLOY_CONFIG": str(self.path), "BACKPROP_TOKEN": "selected-token"}, clear=True), \
             patch("foundry_api.subprocess.run", side_effect=AssertionError("Requested Secret Manager")):
            api = foundry_api.FoundryApi()
        self.assertEqual(api.config.path, self.path.resolve())
        self.assertEqual(api.host, HOST)
        self.assertEqual(api.token, "selected-token")

    def test_no_credential_source_fails_without_using_a_default_cloud_project(self):
        with patch.dict(os.environ, {}, clear=True), \
             patch("foundry_api.subprocess.run", side_effect=AssertionError("Requested default secret")):
            with self.assertRaises(ValueError):
                foundry_api.FoundryApi(self.make_config(secret_manager=False))

    def test_invalid_explicit_tokens_fail_before_credential_or_network_access(self):
        config = self.make_config()
        for token in ("", " ", "line-one\nline-two", "line-one\rline-two"):
            with self.subTest(token=repr(token)), patch.dict(os.environ, {"BACKPROP_TOKEN": "valid-env-token"}, clear=True), \
                 patch("foundry_api.subprocess.run", side_effect=AssertionError("Requested another token")), \
                 patch("foundry_api.urllib.request.build_opener", side_effect=AssertionError("Created network client")):
                with self.assertRaises(ValueError):
                    foundry_api.FoundryApi(config, token=token)

    def test_present_empty_or_multiline_environment_token_does_not_fall_through(self):
        config = self.make_config()
        for name in ("BACKPROP_TOKEN", "FOUNDRY_TOKEN"):
            for token in ("", "first\nsecond"):
                environment = {name: token}
                if name == "BACKPROP_TOKEN":
                    environment["FOUNDRY_TOKEN"] = "valid-fallback-token"
                with self.subTest(name=name, token=repr(token)), patch.dict(os.environ, environment, clear=True), \
                     patch("foundry_api.subprocess.run", side_effect=AssertionError("Requested another token")):
                    with self.assertRaises(ValueError):
                        foundry_api.FoundryApi(config)

    def test_secret_manager_failures_and_invalid_output_fail_explicitly(self):
        config = self.make_config()
        failure = subprocess.CalledProcessError(1, ["gcloud"])
        with patch.dict(os.environ, {}, clear=True), patch("foundry_api.subprocess.run", side_effect=failure):
            with self.assertRaisesRegex(RuntimeError, "Secret Manager"):
                foundry_api.FoundryApi(config)
        for token in ("\n", "first\nsecond\n"):
            with self.subTest(token=repr(token)), patch.dict(os.environ, {}, clear=True), \
                 patch("foundry_api.subprocess.run", return_value=subprocess.CompletedProcess([], 0, token, "")):
                with self.assertRaises(ValueError):
                    foundry_api.FoundryApi(config)

    def test_request_uses_selected_host_and_rejects_foreign_urls(self):
        api = foundry_api.FoundryApi(self.make_config(secret_manager=False), token="fixture-token")
        api.opener = MagicMock()
        response = api.opener.open.return_value.__enter__.return_value
        response.read.return_value = b"{}"
        response.headers = {"Content-Type": "application/json"}
        self.assertEqual(api.request("/api/v2/fixture"), {})
        request = api.opener.open.call_args.args[0]
        self.assertEqual(request.full_url, HOST + "/api/v2/fixture")
        self.assertEqual(request.get_header("Authorization"), "Bearer fixture-token")
        api.opener.reset_mock()
        for path in ("https://other.example.com/api", "//other.example.com/api", "api/v2/fixture"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                api.request(path)
        api.opener.open.assert_not_called()

    def test_redirect_handler_never_creates_a_request_to_another_host(self):
        api = foundry_api.FoundryApi(self.make_config(secret_manager=False), token="fixture-token")
        self.assertTrue(any(isinstance(handler, foundry_api.NoRedirect) for handler in api.opener.handlers))
        original = Request(HOST + "/api/fixture", headers={"Authorization": "Bearer fixture-token"})
        for status in (301, 302, 303, 307, 308):
            with self.subTest(status=status):
                self.assertIsNone(foundry_api.NoRedirect().redirect_request(
                    original, None, status, "redirect", {}, "https://other.example.com/steal"))


if __name__ == "__main__":
    unittest.main()
