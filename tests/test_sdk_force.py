"""force controls login choices, without changing explicit mode or credentials.

Architecture: execute the pinned maybe_login branch with an inert auth boundary;
then initialize actual Backprop runs using native token selection and a memory
uploader. Missing-token tests use the real store constructor and never network.
Main callers: scientific/stdlib SDK tests and the isolated release runtime suite.
"""
import ast
import copy
import hashlib
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from importlib import import_module
from unittest.mock import patch

import backprop
from backprop.foundry import FoundryStore

SOURCE = Path(__file__).resolve().parents[1] / ".reference/wandb/source/wandb/sdk/wandb_init.py"
SOURCE_HASH = "1f38fe5185742bb3609031b4abc4493bb4e5b9ff442607b05825cef8e83017ab"
DEPLOYMENT = {"host": "https://example.invalid", "stream_dataset_rid": "ri.foundry.main.dataset.labels-test"}


class SourceSettings:
    """Only the settings operations read by the unchanged maybe_login body."""
    def __init__(self, mode="online", force=False):
        self.mode, self.force = mode, force
        self.base_url, self.api_key = "https://example.invalid", "explicit-key"
        self.quiet = self.silent = True
    @property
    def _noop(self): return self.mode == "disabled"
    @property
    def _offline(self): return self.mode in {"offline", "dryrun"}
    def model_copy(self): return copy.copy(self)
    def update_from_settings(self, other): self.__dict__.update(other.__dict__)


class ForceContracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.addCleanup(backprop.teardown)
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {}, clear=True).start()
        patch('backprop.settings.default_path', return_value=str(Path(self.temp.name) / 'credentials.json')).start()
        self.sink = backprop.MemorySink()

    def new(self, **kwargs):
        settings = {"console": "off", "silent": True, "disable_git": True, "label_disable": True,
                    **kwargs.pop("settings", {})}
        return backprop.init(project="force", dir=self.temp.name, _deployment=DEPLOYMENT, settings=settings, **kwargs)

    @unittest.skipUnless(SOURCE.is_file(), "Pinned W&B0.30 init source required")
    def test_pinned_offline_skip_and_online_force_passthrough(self):
        data = SOURCE.read_bytes()
        self.assertEqual(hashlib.sha256(data).hexdigest(), SOURCE_HASH)
        tree = ast.parse(data)
        owner = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "_WandbInit")
        method = next(node for node in owner.body if isinstance(node, ast.FunctionDef) and node.name == "maybe_login")
        calls = []
        namespace = {"Settings": SourceSettings, "wandb_login": SimpleNamespace(_login=lambda **kwargs: calls.append(kwargs))}
        exec(compile(ast.Module(body=[method], type_ignores=[]), str(SOURCE), "exec"), namespace)
        instance = SimpleNamespace(_wl=SimpleNamespace(settings=SourceSettings()))
        for force in (False, True):
            for mode in ("offline", "dryrun", "disabled", "online"):
                with self.subTest(force=force, mode=mode):
                    calls.clear(); namespace["maybe_login"](instance, SourceSettings(mode, force))
                    self.assertEqual(len(calls), int(mode == "online"))
                    if calls:
                        self.assertIs(calls[0]["force"], force)
                        self.assertEqual(calls[0]["key"], "explicit-key")

    def test_explicit_and_environment_foundry_credentials_accept_both_flags(self):
        tokens = []
        def native(config, token=None):
            tokens.append(FoundryStore(config, token=token).token)
            return self.sink
        for force in (False, True):
            for source in ("settings", "FOUNDRY_TOKEN", "BACKPROP_TOKEN", "login"):
                with self.subTest(force=force, source=source), patch.dict(os.environ, {}, clear=True), patch.object(import_module("backprop.run"), "native_store", side_effect=native):
                    settings = {"api_key": "explicit-foundry-token"} if source == "settings" else {}
                    if source == "login": backprop.login(key="login-foundry-token", verify=False)
                    elif source != "settings": os.environ[source] = "environment-foundry-token"
                    run = self.new(force=force, settings=settings); run.finish()
                    expected = "explicit-foundry-token" if source == "settings" else "login-foundry-token" if source == "login" else "environment-foundry-token"
                    self.assertEqual(tokens[-1], expected)
                    self.assertIs(run.settings.force, force)
                    self.assertNotIn(expected, repr(self.sink.events))
                    # Each case is a fresh session. Successful login now
                    # updates process defaults, as the pinned SDK does.
                    backprop.teardown()

    def test_offline_dryrun_disabled_skip_store_for_both_flags(self):
        for force in (False, True):
            for mode in ("offline", "dryrun", "disabled"):
                with self.subTest(force=force, mode=mode), patch.object(import_module("backprop.run"), "native_store", side_effect=AssertionError("no store in local modes")):
                    run = self.new(force=force, mode=mode); run.finish()
                    self.assertIs(run.settings.force, force)

    def test_missing_token_fails_before_persistence_without_offline_fallback(self):
        for force in (False, True):
            with self.subTest(force=force), self.assertRaisesRegex(backprop.UsageError, "No Foundry token configured"):
                self.new(force=force)
        self.assertFalse(list(Path(self.temp.name).rglob("events.jsonl")))

    def test_explicit_init_false_overrides_settings_true(self):
        run = self.new(force=False, mode="offline", settings={"force": True}); run.finish()
        self.assertIs(run.settings.force, False)

    def test_login_without_prompt_remains_false_without_credentials(self):
        for force in (False, True):
            with self.subTest(force=force):
                self.assertFalse(backprop.login(force=force, prompt=False))
        self.assertFalse(list(Path(self.temp.name).rglob("*")))


if __name__ == "__main__":
    unittest.main()
