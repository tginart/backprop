"""Login publishes credentials only after successful candidate verification.

Architecture: execute unchanged pinned public/auth method bodies with explicit
inert service dependencies -> compare native environment state and ordering.
Actual stores are replaced before network access. Threaded timeout tests release
their verifier before teardown, proving late completion cannot change a session.
This covers portable state/error behavior, not W&B credential-file persistence.
"""
from __future__ import annotations

import ast
import hashlib
import importlib
import os
from pathlib import Path
import threading
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import backprop


ROOT = Path(__file__).resolve().parents[1]
LOGIN = ROOT / ".reference/wandb/source/wandb/sdk/wandb_login.py"
AUTH = ROOT / ".reference/wandb/source/wandb/sdk/lib/wbauth/authenticate.py"
HASHES = {
    LOGIN: "d8611b98f98fc590bf4984f8c374c0c6689062bf00b0251ca043a347e9595e06",
    AUTH: "5949183f886c46114358924716d02f0400e812341f66b7f016b8a7c9af451813",
}
CONFIG = {"host": "https://old.invalid", "stream_dataset_rid": "ri.foundry.main.dataset.login"}
OLD_ENV = {"BACKPROP_TOKEN": "old-fixture-token", "BACKPROP_HOST": CONFIG["host"]}


def source_function(path, name, namespace):
    """Compile the unchanged pinned body, supplying its declared dependencies."""
    if not path.is_file():
        raise unittest.SkipTest("Pinned W&B0.30 login source is required")
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != HASHES[path]:
        raise AssertionError("Pinned login reference changed")
    node = next(item for item in ast.parse(data).body if isinstance(item, ast.FunctionDef) and item.name == name)
    future = ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)
    module = ast.fix_missing_locations(ast.Module(body=[future, node], type_ignores=[]))
    exec(compile(module, str(path), "exec"), namespace)
    return namespace[name]


class LoginSessionContracts(unittest.TestCase):
    def setUp(self):
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, OLD_ENV, clear=True).start()
        patch.object(backprop, "run", None).start()
        patch.object(backprop, "_setup_settings", None).start()
        self.credential_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.credential_directory.cleanup)
        patch('backprop.settings.default_path', return_value=str(Path(self.credential_directory.name) / 'credentials.json')).start()
        self.settings = backprop.Settings()
        self.resolver_patch = patch.object(backprop, "resolve_settings", return_value=self.settings)
        self.resolver_patch.start()
        self.config = dict(CONFIG)
        self.loader = patch.object(backprop, "load_config", return_value=self.config).start()
        self.factory = patch.object(backprop, "native_store").start()

    def assert_old_session(self):
        self.assertEqual(dict(os.environ), OLD_ENV)
        self.assertEqual(self.config, CONFIG)
        self.assertIsNone(self.settings.api_key)

    def test_missing_credentials_false_preserves_host_like_pinned_public_body(self):
        updates = []
        sentinel = object()
        source = source_function(LOGIN, "login", {
            "UNSET": sentinel, "term": SimpleNamespace(termwarn=lambda *a, **k: None),
            "wandb": SimpleNamespace(run=None),
            "wandb_setup": SimpleNamespace(singleton=lambda: SimpleNamespace(settings=SimpleNamespace(_noop=False, _offline=False))),
            "_login": lambda **kwargs: (False, None),
            "_update_system_settings": lambda *args, **kwargs: updates.append(kwargs),
        })
        self.assertFalse(source(host="https://new.invalid/", prompt=False))
        self.assertEqual(updates, [])
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(backprop.login(host="https://new.invalid/", prompt=False))
            self.assertEqual(dict(os.environ), {})
        self.loader.assert_not_called()
        self.factory.assert_not_called()

    def test_pinned_explicit_key_verification_precedes_session_publication(self):
        for fails in (False, True):
            with self.subTest(fails=fails):
                trace = []
                current = ["old-fixture-token"]
                class AuthKey:
                    def __init__(self, *, host, api_key):
                        self.host, self.api_key = host, api_key
                    def verify(self):
                        trace.append(("verify", current[0]))
                        if fails:
                            raise backprop.AuthenticationError("rejected fixture")
                def publish(auth, **kwargs):
                    trace.append(("publish", auth.api_key)); current[0] = auth.api_key
                source = source_function(LOGIN, "_use_explicit_key", {
                    "wbauth": SimpleNamespace(AuthApiKey=AuthKey, use_explicit_auth=publish),
                })
                kwargs = {"key": "new-fixture-token", "settings": SimpleNamespace(_notebook=False),
                          "host": SimpleNamespace(url="https://new.invalid"), "update_api_key": False,
                          "silent": True, "verify": True}
                if fails:
                    with self.assertRaises(backprop.AuthenticationError): source(**kwargs)
                else:
                    source(**kwargs)
                expected = [("verify", "old-fixture-token")]
                if not fails: expected.append(("publish", "new-fixture-token"))
                self.assertEqual(trace, expected)
                with patch.dict(os.environ, OLD_ENV, clear=True):
                    observations = []
                    def verify():
                        observations.append(dict(os.environ))
                        if fails: raise backprop.AuthenticationError("rejected fixture")
                    self.factory.return_value = SimpleNamespace(verify_credentials=verify)
                    if fails:
                        with self.assertRaises(backprop.AuthenticationError):
                            backprop.login(key="new-fixture-token", host="https://new.invalid/")
                        self.assert_old_session()
                    else:
                        self.assertTrue(backprop.login(key="new-fixture-token", host="https://new.invalid/"))
                        self.assertEqual(os.environ["BACKPROP_TOKEN"], current[0])
                        self.assertEqual(os.environ["BACKPROP_HOST"], "https://new.invalid")
                    self.assertEqual(observations, [OLD_ENV])
                    self.factory.assert_called_with({**CONFIG, "host": "https://new.invalid"}, token="new-fixture-token")
                    self.assertEqual(self.config, CONFIG)

    def test_candidate_validation_failure_preserves_prior_auth_without_store(self):
        cases = [
            {"key": ""}, {"key": " token"}, {"key": 123},
            {"key": "new-fixture-token", "host": "http://invalid.test"},
            {"key": "new-fixture-token", "host": "https://invalid.test/path"},
            {"key": "new-fixture-token", "host": "https://user@invalid.test"},
            {"key": "new-fixture-token", "host": "https://invalid.test?query"},
            {"key": "new-fixture-token", "host": "https://invalid.test:bad"},
        ]
        for kwargs in cases:
            with self.subTest(kwargs=kwargs), self.assertRaises((backprop.AuthenticationError, backprop.UsageError)):
                backprop.login(**kwargs, verify=False)
            self.assert_old_session()
        self.factory.assert_not_called()
        self.loader.assert_not_called()

    def test_failed_store_configuration_and_missing_verifier_preserve_auth(self):
        self.factory.side_effect = ValueError("invalid fixture config")
        with self.assertRaises(ValueError): backprop.login(key="new-fixture-token", host="https://new.invalid")
        self.assert_old_session()
        self.factory.side_effect = None
        self.factory.return_value = object()
        with self.assertRaises(backprop.AuthenticationError): backprop.login(key="new-fixture-token")
        self.assert_old_session()

    def test_unpublishable_candidate_values_preserve_both_credentials_and_defaults(self):
        defaults = backprop.Settings(api_key="old-fixture-token", base_url=CONFIG["host"])
        with patch.object(backprop, "_setup_settings", defaults):
            for host in ("https://invalid\0.test", "https://invalid\n.test", "https://invalid\x7f.test", "https://invalid\ud800.test"):
                with self.subTest(host=ascii(host)), self.assertRaises(backprop.UsageError):
                    backprop.login(key="new-fixture-token", host=host, verify=False)
                self.assert_old_session()
                self.assertIs(backprop._setup_settings, defaults)
            for token in ("new\0fixture-token", "new\ud800fixture-token"):
                with self.subTest(token_kind="unpublishable"), self.assertRaises(backprop.AuthenticationError):
                    backprop.login(key=token, host="https://new.invalid", verify=False)
                self.assert_old_session()
                self.assertIs(backprop._setup_settings, defaults)
        self.factory.assert_not_called()

    def test_timeout_and_late_success_never_publish_candidate(self):
        started, release, finished = threading.Event(), threading.Event(), threading.Event()
        def verify():
            started.set()
            try:
                if not release.wait(2): raise AssertionError("test did not release verifier")
            finally:
                finished.set()
        self.factory.return_value = SimpleNamespace(verify_credentials=verify)
        try:
            with self.assertRaises(TimeoutError):
                backprop.login(key="new-fixture-token", host="https://new.invalid", timeout=.02)
            self.assertTrue(started.is_set())
            self.assert_old_session()
        finally:
            release.set()
            self.assertTrue(finished.wait(2))
        self.assert_old_session()

    def test_verify_false_commits_valid_candidate_without_loading_configuration(self):
        saved_login = backprop.login
        self.assertTrue(saved_login(key="new-fixture-token", host="https://new.invalid/", verify=False))
        self.assertEqual(os.environ["BACKPROP_TOKEN"], "new-fixture-token")
        self.assertEqual(os.environ["BACKPROP_HOST"], "https://new.invalid")
        self.assertIs(backprop.login, saved_login)
        self.assertTrue(saved_login(verify=False))
        self.factory.assert_not_called()
        self.loader.assert_not_called()

    def test_environment_candidate_is_explicit_to_store_and_published_as_session(self):
        for environment in ({"FOUNDRY_TOKEN": "environment-fixture-token"},
                            {"BACKPROP_TOKEN": "preferred-fixture-token", "FOUNDRY_TOKEN": "other-fixture-token"}):
            with self.subTest(environment=sorted(environment)), patch.dict(os.environ, environment, clear=True):
                self.factory.return_value = SimpleNamespace(verify_credentials=lambda: None)
                self.assertTrue(backprop.login())
                self.factory.assert_called_with(CONFIG, token=environment.get("BACKPROP_TOKEN") or environment["FOUNDRY_TOKEN"])
                self.assertEqual(dict(os.environ), {**environment,
                    "BACKPROP_TOKEN": environment.get("BACKPROP_TOKEN") or environment["FOUNDRY_TOKEN"],
                    "BACKPROP_HOST": CONFIG["host"]})

    def test_settings_credentials_match_pinned_environment_precedence_and_host_scope(self):
        class HostUrl:
            def __init__(self, value): self.url = value
            def is_same_url(self, other): return self.url.rstrip("/") == str(getattr(other, "url", other)).rstrip("/")
        for env_key, matching_host in ((None, True), ("environment-fixture-token", True), (None, False)):
            with self.subTest(env_key=bool(env_key), matching_host=matching_host):
                requested = "https://old.invalid" if matching_host else "https://new.invalid"
                selected = SimpleNamespace(api_key="settings-fixture-token", base_url=CONFIG["host"])
                wrapper = lambda *, auth, source: SimpleNamespace(auth=auth, source=source)
                make_auth = lambda *, host, api_key: SimpleNamespace(host=host, api_key=api_key, verify=lambda: None)
                namespace = {
                    "_try_env_auth": lambda **kwargs: wrapper(auth=make_auth(host=kwargs["host"], api_key=env_key), source="env") if env_key else None,
                    "wbnetrc": SimpleNamespace(read_netrc_auth_with_source=lambda **kwargs: None),
                    "wandb_setup": SimpleNamespace(singleton=lambda: SimpleNamespace(settings=selected)),
                    "AuthWithSource": wrapper, "AuthApiKey": make_auth,
                    "_session_auth_lock": threading.Lock(), "_session_auth": None,
                    "term": SimpleNamespace(termlog=lambda *args: None),
                }
                namespace["_locked_set_session_auth"] = lambda auth: namespace.update(_session_auth=auth)
                source = source_function(AUTH, "_use_system_auth", namespace)
                expected = source(host=HostUrl(requested), source="test", verify=True)
                self.settings.api_key = selected.api_key
                self.settings.base_url = selected.base_url
                with patch.dict(os.environ, {"FOUNDRY_TOKEN": env_key} if env_key else {}, clear=True):
                    self.factory.reset_mock()
                    self.factory.return_value = SimpleNamespace(verify_credentials=lambda: None)
                    result = backprop.login(host=requested, prompt=False)
                    self.assertEqual(result, expected is not None)
                    if expected is not None:
                        self.assertEqual(self.factory.call_args.kwargs["token"], expected.api_key)
                    else:
                        self.factory.assert_not_called()
                        self.assertEqual(dict(os.environ), {})

    def test_default_upstream_base_url_is_ignored_but_explicit_one_is_rejected(self):
        self.settings.api_key = "settings-fixture-token"
        self.factory.return_value = SimpleNamespace(verify_credentials=lambda: None)
        with patch.dict(os.environ, {}, clear=True):
            self.assertTrue(backprop.login())
            self.factory.assert_called_with(CONFIG, token="settings-fixture-token")
        self.settings.base_url = "https://api.wandb.ai"
        with self.assertRaises(backprop.UsageError): backprop.login()
        self.assertEqual(dict(os.environ), OLD_ENV)
        self.assertEqual(self.config, CONFIG)
        self.assertEqual(self.settings.api_key, "settings-fixture-token")

    def test_successful_login_handoff_to_init_api_and_explicit_run_override(self):
        self.resolver_patch.stop()
        runtime = importlib.import_module("backprop.run")
        public = importlib.import_module("backprop.apis.public.api")
        for settings_only in (False, True):
            with self.subTest(settings_only=settings_only), tempfile.TemporaryDirectory() as directory:
                backprop.teardown()
                with patch.dict(os.environ, {"BACKPROP_STREAM_DATASET_RID": CONFIG["stream_dataset_rid"]}, clear=True):
                    supplied = backprop.Settings(api_key="old-fixture-token", base_url=CONFIG["host"],
                        console="off", silent=True, disable_git=True, label_disable=True)
                    backprop.setup(supplied)
                    defaults_before = backprop._setup_settings
                    self.factory.return_value = SimpleNamespace(verify_credentials=lambda: None)
                    if settings_only:
                        self.assertTrue(backprop.login())
                        token, host = "old-fixture-token", CONFIG["host"]
                    else:
                        self.assertTrue(backprop.login(key="new-fixture-token", host="https://new.invalid"))
                        token, host = "new-fixture-token", "https://new.invalid"
                    self.assertEqual(supplied.api_key, "old-fixture-token")
                    self.assertEqual(supplied.base_url, CONFIG["host"])
                    # Actual pinned login updates the process-owned Settings
                    # referenced by earlier setup handles after verification.
                    self.assertIs(backprop._setup_settings, defaults_before)
                    self.assertEqual(defaults_before.api_key, token)
                    self.assertEqual(backprop._setup_settings.api_key, token)
                    self.assertEqual(backprop._setup_settings.base_url, host)
                    sink = backprop.MemorySink()
                    with patch.object(runtime, "native_store", return_value=sink) as create:
                        run = backprop.init(project="login-handoff", dir=directory, _deployment=CONFIG)
                        self.assertEqual(create.call_args.kwargs["token"], token)
                        self.assertEqual(create.call_args.args[0]["host"], host)
                        run.finish()
                        run = backprop.init(project="login-handoff", dir=directory, _deployment=CONFIG,
                            settings={"api_key": "per-run-fixture-token", "base_url": "https://per-run.invalid"})
                        self.assertEqual(create.call_args.kwargs["token"], "per-run-fixture-token")
                        self.assertEqual(create.call_args.args[0]["host"], "https://per-run.invalid")
                        run.finish()
                    with patch.object(public, "native_store", return_value=sink) as create, \
                            patch.object(sink, "verify_credentials", create=True) as verify:
                        api = backprop.Api()
                        verify.assert_called_once_with()
                        self.assertEqual(api.api_key, token)
                        self.assertEqual(create.call_args.kwargs["token"], token)
                        self.assertEqual(create.call_args.args[0]["host"], host)
                    self.assertNotIn(token, repr(sink.events))
                backprop.teardown()

    def test_failed_verification_preserves_setup_object_and_fields(self):
        supplied = backprop.Settings(api_key="old-fixture-token", base_url=CONFIG["host"], project="keep-project")
        with patch.object(backprop, "_setup_settings", supplied):
            self.factory.return_value = SimpleNamespace(verify_credentials=lambda: (_ for _ in ()).throw(backprop.AuthenticationError("rejected")))
            with self.assertRaises(backprop.AuthenticationError):
                backprop.login(key="new-fixture-token", host="https://new.invalid")
            self.assertIs(backprop._setup_settings, supplied)
            self.assertEqual(supplied.api_key, "old-fixture-token")
            self.assertEqual(supplied.base_url, CONFIG["host"])
            self.assertEqual(supplied.project, "keep-project")
            self.assert_old_session()

    def test_missing_credentials_prompt_true_matches_pinned_usage_error(self):
        class NotATerminalError(Exception): pass
        class HostUrl:
            def __init__(self, value): self.value = value
        def cannot_prompt(**kwargs): raise NotATerminalError()
        source = source_function(AUTH, "authenticate_session", {
            "HostUrl": HostUrl, "session_credentials": lambda **kwargs: None,
            "_use_system_auth": lambda **kwargs: None, "_use_prompted_auth": cannot_prompt,
            "term": SimpleNamespace(NotATerminalError=NotATerminalError), "UsageError": backprop.UsageError,
        })
        with self.assertRaises(backprop.UsageError): source(host="https://example.invalid", source="test")
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(backprop.UsageError): backprop.login()
            self.assertEqual(dict(os.environ), {})
        self.factory.assert_not_called()

    def test_active_and_local_modes_ignore_new_credentials_without_side_effects(self):
        for mode in ("disabled", "offline", "dryrun"):
            with self.subTest(mode=mode):
                self.settings.mode = mode
                self.assertFalse(backprop.login(key="new-fixture-token", host="https://new.invalid"))
                self.assert_old_session()
        self.settings.mode = "online"
        with patch.object(backprop, "run", object()), patch.object(backprop, "termwarn"):
            self.assertFalse(backprop.login(key="new-fixture-token", host="https://new.invalid"))
        self.assert_old_session()
        self.factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
