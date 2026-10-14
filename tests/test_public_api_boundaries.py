"""Verify public call error boundaries against unchanged W&B0.30 wrappers.

Architecture: hash-checked source methods with their real decorator -> supplied
read/download dependencies -> native API calls. Tests distinguish eager failure
from later collection iteration and check that failed downloads preserve bytes.
The source service transport predicate is compared to an explicit Foundry status
mapping, without running or claiming the separate W&B service protocol.
"""
from __future__ import annotations

import ast
from http import HTTPStatus
import hashlib
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from backprop.apis.public import Api
from backprop.apis.public.artifacts import ArtifactType, ArtifactCollection
from backprop.apis.public.files import File, safe_destination
from backprop.apis.public.projects import Project
from backprop.apis.public.runs import RunNotFoundError
from backprop.errors import CommError, Error, UsageError
from backprop.foundry import FoundryError
from backprop.transport import MemorySink
from test_public_api_errors import ROOT, reference_normalize, supplied_requests

HASHES = {
    "api.py": "976ea3b791d8eac2626186c75095249d0838a3328efaee5191a9d48479a3f025",
    "files.py": "b4142a3c3c656e753a40dcd99cd415fc1ab02a0b7a2bdc3083e4f0d5aedd6628",
    "projects.py": "794db2b2710ab1a5b04e8a968f5048245ceecacde6f5391a01d123ec08b13ded",
    "artifacts.py": "7fd34672b80ce1610368ea72dfeb26f9ddf27226f45f0d7d7a93d33ff41e7dc0",
}
SCOPES = {
    ("api.py", Api): ("run", "artifact_types", "artifact_type", "artifact_collections",
                       "artifact_collection", "artifact_versions", "artifacts", "artifact",
                       "artifact_exists", "artifact_collection_exists"),
    ("files.py", File): ("download",),
    ("artifacts.py", ArtifactType): ("collections",),
    ("artifacts.py", ArtifactCollection): ("artifacts",),
    ("projects.py", Project): ("artifacts_types", "collections"),
}


def parsed_source(name):
    path = ROOT / "wandb/apis/public" / name
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != HASHES[name]:
        raise AssertionError(f"Unexpected pinned W&B0.30 source: {path}")
    return ast.parse(data), str(path)


def source_methods(filename, class_name, names, globals=None, functions=()):
    """Compile selected method bodies, retaining every original decorator.

    Main callers: existence and File differential tests. Explicit dependencies
    supply storage operations only; the tested control flow is not rewritten.
    """
    tree, path = parsed_source(filename)
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name)
    nodes = [node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name in names]
    nodes += [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in functions]
    namespace = {"normalize_exceptions": reference_normalize(), **(globals or {})}
    module = ast.fix_missing_locations(ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), *nodes], type_ignores=[]))
    exec(compile(module, path, "exec"), namespace)
    return namespace


class PublicBoundaryContracts(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.sink = MemorySink()
        self.api = Api._from_store(self.sink, entity="team", project="project")

    def test_actual_eager_validation_boundaries_normalize(self):
        kind = ArtifactType(self.api, "team", "project", "dataset")
        collection = ArtifactCollection(self.api, "team", "project", "data", "dataset", [])
        project = Project(self.api, "team", "project", [])
        calls = [lambda: self.api.run(None), lambda: self.api.artifact_types(5),
                 lambda: self.api.artifact_type("dataset", 5),
                 lambda: self.api.artifact_collections(5, "dataset"),
                 lambda: self.api.artifact_collection("dataset", None),
                 lambda: self.api.artifact_versions("dataset", "data", per_page=0),
                 lambda: self.api.artifacts("dataset", "data", tags="not a list"),
                 lambda: self.api.artifact(None),
                 lambda: kind.collections(filters={"bad": {"$not_supported": 1}}),
                 lambda: collection.artifacts(per_page=0),
                 lambda: project.artifacts_types(per_page=0),
                 lambda: project.collections(filters={"bad": {"$not_supported": 1}})]
        for call in calls:
            with self.subTest(call=call), self.assertRaises(CommError) as caught:
                call()
            self.assertIsInstance(caught.exception.exc, ValueError)

    def test_lazy_reads_fail_during_iteration_without_call_normalization(self):
        kind = ArtifactType(self.api, "team", "project", "dataset")
        collection = ArtifactCollection(self.api, "team", "project", "data", "dataset", [])
        project = Project(self.api, "team", "project", [])
        failure = OSError("read failed after collection was returned")
        factories = [lambda: self.api.artifact_types(),
                     lambda: self.api.artifact_collections("project", "dataset"),
                     lambda: self.api.artifact_versions("dataset", "data"),
                     lambda: self.api.artifacts("dataset", "data"),
                     kind.collections, collection.artifacts, project.artifacts_types, project.collections]
        with patch.object(self.sink, "read_events", side_effect=failure) as read:
            for factory in factories:
                collection = factory()
                count = read.call_count
                with self.subTest(factory=factory), self.assertRaises(OSError) as caught:
                    list(collection)
                self.assertIs(caught.exception, failure)
                self.assertEqual(read.call_count, count + 1)
            for call in (lambda: self.api.run("run"), lambda: self.api.artifact_type("dataset"),
                         lambda: self.api.artifact_collection("dataset", "data"), lambda: self.api.artifact("data:sha256-test")):
                with self.subTest(call=call), self.assertRaises(CommError) as caught:
                    call()
                self.assertIs(caught.exception.exc, failure)

    def test_missing_run_is_wrapped_but_retains_original_exception(self):
        with self.assertRaises(CommError) as caught:
            self.api.run("missing")
        self.assertIsInstance(caught.exception.exc, RunNotFoundError)
        self.assertEqual(self.api._cache, {})
        with patch.dict(os.environ, {"WANDB_DEBUG": "true"}), self.assertRaises(RunNotFoundError):
            self.api.run("missing")

    def test_existence_validation_missing_and_debug_follow_boolean_contract(self):
        for call in (lambda: self.api.artifact_exists(None),
                     lambda: self.api.artifact_collection_exists(None, "dataset"),
                     lambda: self.api.artifact_exists("missing:sha256-test"),
                     lambda: self.api.artifact_collection_exists("missing", "dataset")):
            self.assertFalse(call())
            with patch.dict(os.environ, {"WANDB_DEBUG": "true"}), self.assertRaises(ValueError):
                call()
        artifact = SimpleNamespace(type="dataset")
        event = {"payload": {"name": "data", "version": "sha256-test"}, "run_id": "run"}
        with patch.object(self.api, "_artifact_events", return_value=[event]), patch.object(self.api, "_make_artifact", return_value=artifact):
            self.assertTrue(self.api.artifact_exists("data:sha256-test", "dataset"))
            self.assertFalse(self.api.artifact_exists("data:sha256-test", "wrong"))
            with patch.dict(os.environ, {"WANDB_DEBUG": "true"}), self.assertRaises(ValueError):
                self.api.artifact_exists("data:sha256-test", "wrong")

    def test_failed_download_preserves_existing_bytes_and_error_cause(self):
        with tempfile.TemporaryDirectory() as root:
            target = Path(root) / "file.txt"
            target.write_text("previous verified data")
            failure = OSError("provider unavailable")
            file = File(self.sink, "file.txt", {})
            with patch.object(self.sink, "download_file", side_effect=failure) as download:
                with self.assertRaises(CommError) as caught:
                    file.download(root, replace=True)
                self.assertIs(caught.exception.exc, failure)
                self.assertEqual(target.read_text(), "previous verified data")
                with self.assertRaises(CommError) as caught:
                    File(self.sink, "../escape", {}).download(root)
                self.assertIsInstance(caught.exception.exc, ValueError)
                self.assertEqual(download.call_count, 1)
            self.assertEqual(sorted(path.name for path in Path(root).iterdir()), ["file.txt"])

    def test_existing_noncommunication_errors_preserve_identity(self):
        for failure in (Error("root SDK error"), UsageError("usage error")):
            with patch.object(self.sink, "read_events", side_effect=failure):
                for call in (lambda: self.api.run("run"), lambda: self.api.artifact_exists("data:v"),
                             lambda: self.api.artifact_collection_exists("data", "dataset")):
                    with self.assertRaises(type(failure)) as caught:
                        call()
                    self.assertIs(caught.exception, failure)


@unittest.skipUnless(all((ROOT / "wandb/apis/public" / name).is_file() for name in HASHES), "Pinned W&B0.30.0 source required")
class PinnedPublicBoundaryContracts(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_exact_fifteen_assigned_decorators_match_source(self):
        count = 0
        for (filename, cls), names in SCOPES.items():
            tree, _ = parsed_source(filename)
            source_class = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == cls.__name__)
            for name in names:
                method = next(node for node in source_class.body if isinstance(node, ast.FunctionDef) and node.name == name)
                self.assertIn("normalize_exceptions", [ast.unparse(dec) for dec in method.decorator_list])
                self.assertTrue(hasattr(getattr(cls, name), "__wrapped__"), f"{cls.__name__}.{name}")
                count += 1
        self.assertEqual(count, 15)
        for cls, names in ((Api, ("runs", "projects", "project", "from_path")),
                           (ArtifactType, ("collection", "load")), (ArtifactCollection, ("load",))):
            for name in names:
                self.assertFalse(hasattr(getattr(cls, name), "__wrapped__"), f"{cls.__name__}.{name}")

    def test_actual_decorated_existence_bodies_match_error_and_transport_results(self):
        class SourceTransportError(Exception):
            def __init__(self, status):
                self.response = None if status is None else SimpleNamespace(http_status=status)
        namespace = source_methods("api.py", "Api", {"artifact_exists", "artifact_collection_exists"},
                                   {"wandb": SimpleNamespace(errors=SimpleNamespace(CommError=CommError)),
                                    "WandbApiFailedError": SourceTransportError, "HTTPStatus": HTTPStatus},
                                   ("_api_error_status", "_is_service_api_transport_error"))
        factories = [lambda: None, lambda: ValueError("bad name/type"), lambda: CommError("response"),
                     lambda: OSError("ordinary provider error"), lambda: Error("other SDK Error"),
                     lambda: UsageError("usage error"), lambda: KeyboardInterrupt()]
        for status in (None, 408, 504, 400, 401, 403, 404, 409, 429, 500, 503):
            factories.append(lambda status=status: (CommError("Foundry response", SourceTransportError(status)),
                                                   FoundryError("Foundry response", status_code=status)))
        for method in ("artifact_exists", "artifact_collection_exists"):
            for debug in ("false", "true"):
                for factory in factories:
                    values = factory()
                    errors = values if isinstance(values, tuple) else (values, values)
                    outcomes = []
                    for expected, failure in zip((True, False), errors):
                        def lookup(*args):
                            if failure is not None: raise failure
                            return object()
                        receiver = SimpleNamespace()
                        # Use the actual decorator on the inner lookup too;
                        # source _artifact and artifact_collection both have it.
                        decorator = reference_normalize() if expected else __import__("backprop.apis.normalize", fromlist=["normalize_exceptions"]).normalize_exceptions
                        receiver._artifact = receiver.artifact = receiver.artifact_collection = decorator(lookup)
                        operation = namespace[method] if expected else getattr(Api, method)
                        with supplied_requests(), patch.dict(os.environ, {"WANDB_DEBUG": debug}):
                            try:
                                result = operation(receiver, "data:version", "dataset")
                                outcomes.append(("return", result))
                            except BaseException as caught:
                                # FoundryError is the native CommError subtype.
                                outcomes.append(("error", CommError if isinstance(caught, CommError) else type(caught), str(caught)))
                    with self.subTest(method=method, debug=debug, failure=errors):
                        self.assertEqual(outcomes[0], outcomes[1])

    def test_actual_decorated_file_download_preserves_return_and_error_types(self):
        namespace = source_methods("files.py", "File", {"download"},
                                   {"os": os, "validate_fspath": lambda root, name: str(safe_destination(root, name)),
                                    "ApiRequest": SimpleNamespace, "DownloadFileRequest": SimpleNamespace})
        for scenario in ("success", "existing", "exist_ok", "provider_failure"):
            for debug in ("false", "true"):
                outcomes = []
                for expected in (True, False):
                    with tempfile.TemporaryDirectory() as root:
                        target = Path(root) / "file.txt"
                        if scenario in {"existing", "exist_ok"}: target.write_text("already here")
                        def download(*args):
                            if scenario == "provider_failure": raise OSError("provider failed")
                            return b"downloaded bytes"
                        def service(request):
                            Path(request.download_file_request.path).write_bytes(download())
                        receiver = SimpleNamespace(name="file.txt", url="https://example.invalid/file", size=16,
                                                   _service_api=SimpleNamespace(send_api_request=service)) if expected else File(SimpleNamespace(download_file=download), "file.txt", {})
                        operation = namespace["download"] if expected else File.download
                        with supplied_requests(), patch.dict(os.environ, {"WANDB_DEBUG": debug}):
                            try:
                                with operation(receiver, root, exist_ok=scenario == "exist_ok") as stream:
                                    outcomes.append(("return", type(stream), stream.read()))
                            except Exception as caught:
                                outcomes.append(("error", type(caught), type(getattr(caught, "exc", None))))
                with self.subTest(scenario=scenario, debug=debug):
                    self.assertEqual(outcomes[0], outcomes[1])


if __name__ == "__main__": unittest.main()
