"""Compare real pinned public error normalization with supplied dependencies.

Architecture: hash-checked decorator/environment/parser bodies -> explicit HTTP
and excluded service-protocol dependency classes -> compare native exceptions,
attributes, debug behavior and lazy timing. Requests fixtures model its exception
interface only; no HTTP request, installed provider or W&B service is exercised.
Main callers: public-boundary regressions and dataframe reference comparisons.
"""
from __future__ import annotations

import ast
from contextlib import contextmanager
from functools import wraps
import os
from pathlib import Path
import hashlib
import sys
import tempfile
from types import ModuleType, SimpleNamespace
from typing import cast, TypeVar
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sdk"))
import backprop
from backprop.errors import CommError, Error
from backprop.apis.normalize import normalize_exceptions
from backprop.apis.public.runs import Run, Runs

ROOT = Path(__file__).resolve().parents[1] / ".reference/wandb/source"
HASHES = {
    "wandb/apis/normalize.py": "982750838fa149c26f820303d2e34ebf8f0ab6be785d531df7ca822f25d8cbe5",
    "wandb/env.py": "00e18e1a713c6408ab25e31d03072b2d06e78b17500c0ba3112b9ca494e429c8",
    "wandb/errors/errors.py": "f9e01b704c61c0e489c088ed0f706a4a44b8f02ed5653075e71913c0f8d14ab1",
    "wandb/util.py": "414dd36708ea72b8429074b556f61962c03ce06bb7268570467051405bcfcb27",
}


def source_nodes(filename, names, namespace):
    data = (ROOT / filename).read_bytes()
    if hashlib.sha256(data).hexdigest() != HASHES[filename]:
        raise AssertionError(f"Unexpected pinned W&B0.30 source: {filename}")
    nodes = [node for node in ast.parse(data).body if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in names]
    module = ast.fix_missing_locations(ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), *nodes], type_ignores=[]))
    exec(compile(module, str(ROOT / filename), "exec"), namespace)


class SuppliedServiceError(Exception):
    """Compile-time stand-in; no W&B service-protocol branch is claimed/tested."""


class SuppliedHTTPError(Exception):
    def __init__(self, response):
        super().__init__("request failed")
        self.response = response


class SuppliedJSONError(ValueError):
    pass


@contextmanager
def supplied_requests():
    """Supply the actual dependency names imported inside the pinned decorator."""
    module = ModuleType("requests")
    module.HTTPError, module.JSONDecodeError = SuppliedHTTPError, SuppliedJSONError
    with patch.dict(sys.modules, {"requests": module}):
        yield module


def reference_normalize():
    """Return the unchanged pinned decorator with explicit dependency globals.

    Main callers: this file, histories comparisons and API/File boundary tests.
    Call under supplied_requests() in dependency-free tests because the pinned
    wrapper unconditionally imports requests. Public Error aliases are supplied
    as globals; their constructor bodies are independently compared below.
    """
    env = {"os": os, "DEBUG": "WANDB_DEBUG"}
    source_nodes("wandb/env.py", {"strtobool", "_env_as_bool", "is_debug"}, env)
    utility = {}
    source_nodes("wandb/util.py", {"parse_backend_error_messages"}, utility)
    namespace = {"ast": ast, "sys": sys, "wraps": wraps, "cast": cast, "_F": TypeVar("_F"),
                 "env": SimpleNamespace(is_debug=env["is_debug"]),
                 "CommError": CommError, "Error": Error,
                 "WandbApiFailedError": SuppliedServiceError,
                 "parse_backend_error_messages": utility["parse_backend_error_messages"]}
    source_nodes("wandb/apis/normalize.py", {"normalize_exceptions"}, namespace)
    return namespace["normalize_exceptions"]


def raised(error):
    def operation():
        raise error
    return operation


@unittest.skipUnless(all((ROOT / path).is_file() for path in HASHES), "Pinned W&B0.30.0 source snapshot required")
class PublicErrorContracts(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def compare(self, factory, debug=None):
        outcomes = []
        for decorator in (reference_normalize(), normalize_exceptions):
            error = factory()
            with supplied_requests(), patch.dict(os.environ, {} if debug is None else {"WANDB_DEBUG": debug}):
                try:
                    decorator(raised(error))()
                except BaseException as result:
                    outcomes.append((type(result), str(result), getattr(result, "message", None),
                                     getattr(result, "exc", None) is error, result is error,
                                     getattr(result, "context", None)))
                else:
                    self.fail("Expected exception")
        self.assertEqual(outcomes[0], outcomes[1])
        return outcomes[1]

    def test_ordinary_exceptions_payload_parser_and_debug_values_match_pinned(self):
        for value in (None, "1", "true", "YES", "on", "t", "y", "0", "false", "no", "garbage", ""):
            for factory in (lambda: ValueError("bad input"), lambda: RuntimeError(),
                            lambda: TypeError("{'message': 'payload failure'}"),
                            lambda: ValueError("{malformed"), lambda: ValueError("{'other': 1}")):
                with self.subTest(debug=value, error=factory()):
                    self.compare(factory, value)

    def test_existing_sdk_error_and_base_exception_pass_through_unchanged(self):
        for factory in (lambda: Error("existing", {"field": "value"}),
                        lambda: CommError("existing", ValueError("cause")),
                        lambda: KeyboardInterrupt(), lambda: SystemExit(3)):
            for debug in (None, "true"):
                result = self.compare(factory, debug)
                self.assertTrue(result[4])

    def test_http_response_shapes_normalize_even_when_debug_is_enabled(self):
        def response(payload, text="", reason="", invalid_json=False):
            def read():
                if invalid_json: raise SuppliedJSONError("invalid")
                return payload
            return SimpleNamespace(status_code=418, json=read, text=text, reason=reason)
        values = [response({"error": "one"}), response({"error": {"message": "two"}}),
                  response({"errors": ["one", {"message": "two"}, {"message": ""}, 3]}),
                  response({}, text="body"), response({}, reason="Teapot"), response({}),
                  response([], text="not dict"), response(None, text="not json", invalid_json=True)]
        for value in values:
            for debug in (None, "true"):
                result = self.compare(lambda: SuppliedHTTPError(value), debug)
                self.assertIs(result[0], CommError)
                self.assertTrue(result[3])

    def test_error_and_commerror_fields_match_pinned_constructor_bodies(self):
        namespace = {}
        source_nodes("wandb/errors/errors.py", {"Error", "CommError"}, namespace)
        inner = ValueError("original")
        context = {"path": "local"}
        for expected, actual in ((namespace["Error"]("failure", context), Error("failure", context)),
                                 (namespace["CommError"]("failure", inner), CommError("failure", inner)),
                                 (namespace["CommError"]("failure"), CommError("failure"))):
            self.assertEqual(actual.args, expected.args)
            self.assertEqual(actual.message, expected.message)
            self.assertEqual(actual.context, expected.context)
            self.assertIs(getattr(actual, "exc", None), getattr(expected, "exc", None))

    def test_public_exception_hierarchy_matches_pinned_catch_behavior(self):
        from backprop import errors
        from backprop.transport import BackpropError
        names = {"Error", "CommError", "UsageError", "UnsupportedError", "AuthenticationError"}
        namespace = {}
        source_nodes("wandb/errors/errors.py", names, namespace)
        self.assertIs(errors.Error, BackpropError)
        self.assertIs(backprop.Error, BackpropError)
        for name in names:
            expected, actual = namespace[name], getattr(errors, name)
            for ancestor in names:
                self.assertEqual(issubclass(actual, getattr(errors, ancestor)), issubclass(expected, namespace[ancestor]), (name, ancestor))
            for builtin in (Exception, RuntimeError, ValueError):
                self.assertEqual(issubclass(actual, builtin), issubclass(expected, builtin), (name, builtin))

    def test_returned_generator_failures_remain_outside_the_call_decorator(self):
        for decorator in (reference_normalize(), normalize_exceptions):
            def generate():
                yield 1
                raise ValueError("later")
            with supplied_requests():
                result = decorator(generate)()
                self.assertEqual(next(result), 1)
                with self.assertRaisesRegex(ValueError, "later"):
                    next(result)

    def test_real_run_entry_points_normalize_call_errors_but_not_lazy_fetches(self):
        with tempfile.TemporaryDirectory() as root:
            sink = backprop.MemorySink()
            writer = backprop.Run(project="public-errors", id="one", dir=root,
                                  _sink=sink, settings={"silent": True, "console": "off"})
            writer.finish()
            reader = backprop.Api._from_store(sink, project="public-errors").run("one")
            operations = [lambda: reader.history(samples=0), lambda: reader.scan_history(keys="bad"),
                          lambda: reader.files(names="bad"), lambda: reader.file("missing"),
                          lambda: reader.logged_artifacts(per_page=0), lambda: reader.used_artifacts(per_page=0)]
            for operation in operations:
                with self.assertRaises(CommError) as failure:
                    operation()
                self.assertIsInstance(failure.exception.exc, ValueError)
            underlying = ValueError("refresh failed")
            waiting = SimpleNamespace(state="running", load=lambda **kwargs: (_ for _ in ()).throw(underlying))
            with patch("backprop.apis.public.runs.time.sleep"), self.assertRaises(CommError) as failure:
                Run.wait_until_finished(waiting)
            self.assertIs(failure.exception.exc, underlying)
            collection = reader.logged_artifacts()
            with patch.object(reader, "load", side_effect=underlying):
                with self.assertRaises(ValueError) as failure:
                    list(collection)
            self.assertIs(failure.exception, underlying)

    def test_only_pinned_run_entry_points_have_the_decorator(self):
        self.assertTrue(hasattr(Runs.histories, "__wrapped__"))
        for name in ("history", "scan_history", "files", "file", "logged_artifacts", "used_artifacts", "wait_until_finished"):
            self.assertTrue(hasattr(getattr(Run, name), "__wrapped__"), name)
        for name in ("load", "load_full_data", "console_logs", "download_history_exports", "beta_scan_history"):
            self.assertFalse(hasattr(getattr(Run, name), "__wrapped__"), name)


if __name__ == "__main__": unittest.main()
