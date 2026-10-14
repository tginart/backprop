"""Requirement parsing compared directly with the pinned dependency-free module.

Architecture: load the unmodified v0.30.0 module with diagnostic-only stand-ins
for its imports -> run identical arguments -> compare return values, warnings
and exception types/messages. No installed W&B, credentials or network needed.
Main callers: unittest discovery and compatibility release checks.
"""
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sdk"))
import backprop
from backprop.errors import UnsupportedError


class RequireContracts(unittest.TestCase):
    def invoke(self, callable, warnings, *args, **kwargs):
        try:
            result = callable(*args, **kwargs)
        except Exception as error:
            return type(error).__name__, str(error), list(warnings)
        return result, list(warnings)

    def test_pinned_require_noop_alias_tags_iterables_and_error_order(self):
        source = Path(__file__).resolve().parents[1] / ".reference/wandb/source/wandb/sdk/wandb_require.py"
        if not source.exists():
            self.skipTest("Pinned source fixture unavailable")
        reference_warnings = []
        namespace = {}
        fake = SimpleNamespace(termwarn=reference_warnings.append)
        with patch.dict(sys.modules, {"wandb": fake, "wandb.errors": SimpleNamespace(UnsupportedError=UnsupportedError)}):
            exec(compile(source.read_text(), str(source), "exec"), namespace)
        cases = [((), {}), ((None,), {}), (("require",), {}), (("core",), {}),
                 (("service@beta:variant",), {}), (("core:variant@beta",), {}),
                 ((), {"experiment": "core"}), (([],), {"experiment": "service"}),
                 (("require",), {"experiment": "unknown"}),
                 ((["core", "unsupported-one", "service", "unsupported-two@beta"],), {}),
                 (([1],), {})]
        for args, kwargs in cases:
            with self.subTest(args=args, kwargs=kwargs):
                reference_warnings.clear()
                expected = self.invoke(namespace["require"], reference_warnings, *args, **kwargs)
                messages = []
                with patch.object(backprop, "termwarn", side_effect=messages.append):
                    actual = self.invoke(backprop.require, messages, *args, **kwargs)
                self.assertEqual(actual, expected)

    def test_generator_supported_and_error_identity_shared_by_shim(self):
        messages = []
        with patch.object(backprop, "termwarn", side_effect=messages.append):
            self.assertIsNone(backprop.require(name for name in ["require", "core"]))
            with self.assertRaisesRegex(UnsupportedError, "unknown"):
                backprop.require("unknown")
        self.assertEqual(len(messages), 2)
        from backprop.compatibility import install_wandb_aliases
        install_wandb_aliases()
        from wandb.errors import UnsupportedError as CompatibleError
        self.assertIs(CompatibleError, UnsupportedError)
        self.assertIs(backprop.sdk.require, backprop.require)


if __name__ == "__main__":
    unittest.main()
