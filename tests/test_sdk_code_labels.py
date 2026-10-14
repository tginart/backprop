"""Pinned label parsing plus real initialization, persistence and resume behavior.

Architecture: hash-checked source functions provide the parser/probe oracle;
MemorySink then exercises our complete metadata/file/public-readback boundary.
Only notebook discovery/provider APIs are simulated. No optional package or
hosted notebook service is needed, and no test sends network requests.
Main callers: SDK discovery and the release's isolated notebook runtime suite.
"""
import ast
import contextlib
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import backprop
from backprop.code_labels import labels_from_lines, notebook_lines, parse_label_lines, project_rawconfig

SOURCE = Path(__file__).resolve().parents[1] / ".reference/wandb/source/wandb/sdk"
HASHES = {
    "lib/telemetry.py": "3376172cb8a77b2476930a06f3a07c5df96ff54991cfe4ef8c69407256d3866f",
    "wandb_run.py": "b54c1b7a7b4d6adc524a588c0220f6fe51a4ee8e4ee2d2b87708e0a87398de23",
}


def source_functions():
    """Execute exact pure source parser and Run probe bodies, without W&B I/O."""
    trees = {}
    for name, digest in HASHES.items():
        path = SOURCE / name
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != digest:
            raise AssertionError(f"Pinned source changed: {name}")
        trees[name] = ast.parse(data)
    parser = next(node for node in trees["lib/telemetry.py"].body
                  if isinstance(node, ast.FunctionDef) and node.name == "_parse_label_lines")
    namespace = {"re": re, "sys": sys, "Any": object, "RE_LABEL": re.compile(r"[a-zA-Z0-9_-]+$"),
                 "_LABEL_TOKEN": "@wandbcode{", "MATCH_RE": re.compile(r"(?P<code>[a-zA-Z0-9_-]+)[,}](?P<rest>.*)"),
                 "logger": SimpleNamespace(info=lambda *args: None)}
    exec(compile(ast.Module(body=[parser], type_ignores=[]), str(SOURCE / "lib/telemetry.py"), "exec"), namespace)
    @contextlib.contextmanager
    def telemetry_context(run):
        yield SimpleNamespace(label=run.label)
    namespace["telemetry"] = SimpleNamespace(_parse_label_lines=namespace["_parse_label_lines"], context=telemetry_context)
    original = next(node for node in trees["wandb_run.py"].body if isinstance(node, ast.ClassDef) and node.name == "Run")
    names = {"_label_internal", "_label_probe_lines", "_label_probe_main", "_label_probe_notebook"}
    methods = [node for node in original.body if isinstance(node, ast.FunctionDef) and node.name in names]
    wrapper = ast.ClassDef(name="ReferenceRun", bases=[], keywords=[], body=methods, decorator_list=[])
    exec(compile(ast.fix_missing_locations(ast.Module(body=[wrapper], type_ignores=[])), str(SOURCE / "wandb_run.py"), "exec"), namespace)
    return namespace["_parse_label_lines"], namespace["ReferenceRun"]


def notebook(*sources):
    return {"nbformat": 4, "nbformat_minor": 5, "metadata": {}, "cells": [
        {"cell_type": "code", "metadata": {}, "source": source, "outputs": [], "execution_count": None}
        for source in sources]}


@unittest.skipUnless(all((SOURCE / name).is_file() for name in HASHES), "Pinned W&B0.30 source required for exact label differential")
class LabelSourceDifferential(unittest.TestCase):
    def test_parser_and_selected_labels_match_pinned_source(self):
        parser, source_run = source_functions()
        cases = [[], ["nothing"], ["before", "prefix @wandbcode{hello-world,r=my-repo,v=one}"],
                 ["@wandbcode{}", "junk", "@wandbcode{ignored}"],
                 ['@wandbcode{c=short,code=long,r=small,repo=large,v=old,version=new}'],
                 ['@wandbcode{c=short,code="",r=small,repo="",v=old,version=""}'],
                 ['@wandbcode{hello,junk=2}', '@wandbcode{repo="other-repo"}'],
                 ['@wandbcode{h,j="i,e",p=hhh}'], ['@wandbcode{j=i-p,'],
                 ['@wandbcode{code="has space",r=valid}'],
                 ['@wandbcode{first}', 'not a marker', '@wandbcode{second}'],
                 ['@wandbcode{code=first,c=second,code=last}'], ['@wandbcode{é,v=ok}']]
        for lines in cases:
            with self.subTest(lines=lines):
                self.assertEqual(parse_label_lines(lines), parser(lines))
                oracle = source_run(); oracle.label = SimpleNamespace()
                oracle._label_probe_lines(lines)
                mapping = {"code_string": "code", "repo_string": "repo", "code_version": "code_version"}
                expected = {mapping[key]: value for key, value in vars(oracle.label).items()}
                self.assertEqual(labels_from_lines(lines), expected)
        self.assertEqual(len(cases), 13)

    def test_first_cell_and_string_split_match_pinned_probe(self):
        _, source_run = source_functions()
        cases = [notebook(['@wandbcode{first, r=repository}', 'text'], ['@wandbcode{second}']),
                 notebook('@wandbcode{first, r=repository}', '@wandbcode{second}'),
                 notebook('text', '@wandbcode{second}'), notebook(),
                 notebook(None), {"cells": [1]}]
        for document in cases:
            with self.subTest(document=document):
                oracle = source_run(); oracle.label = SimpleNamespace()
                oracle._label_probe_notebook(SimpleNamespace(probe_ipynb=lambda: document))
                try:
                    lines = notebook_lines(document)
                    actual = labels_from_lines(lines) if lines else {}
                except (TypeError, AttributeError, IndexError):
                    actual = {}
                mapping = {"code_string": "code", "repo_string": "repo", "code_version": "code_version"}
                self.assertEqual(actual, {mapping[key]: value for key, value in vars(oracle.label).items()})


class CodeLabelLifecycle(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.addCleanup(backprop.teardown)
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {}, clear=True).start()
        patch.dict(sys.modules, {"IPython": None, "google.colab": None, "kaggle_session": None}).start()
        patch.object(sys.modules["__main__"], "__doc__", "@wandbcode{script,r=repository,v=version}").start()
        self.sink = backprop.MemorySink()

    def new(self, **kwargs):
        settings = {"console": "off", "silent": True, "disable_git": True, **kwargs.pop("settings", {})}
        return backprop.init(project="labels", entity="team", dir=self.temp.name,
                             _sink=self.sink, _deployment={}, settings=settings, **kwargs)

    def public(self, run):
        return backprop.Api._from_store(self.sink, entity="team", project="labels", config={}).run(run.path)

    def shell(self, path=None):
        namespace = {"__vsc_ipynb_file__": str(path)} if path else {}
        shell = SimpleNamespace(kernel=SimpleNamespace(shell=SimpleNamespace(user_ns=namespace)), config={})
        patch.dict(sys.modules, {"IPython": SimpleNamespace(get_ipython=lambda: shell)}).start()
        patch("backprop.notebook_discovery._from_servers", return_value=None).start()
        return shell

    def test_script_metadata_saved_bytes_and_public_rawconfig(self):
        config = {"learning_rate": .1, "_wandb": {"code_path": "existing.py", "pinned_keys": ["learning_rate"],
                                                    "t": {"12": "other-value", "9": {"3": "stale"}}}}
        run = self.new(config=config); run.finish(); remote = self.public(run)
        expected = {"code": "script", "repo": "repository", "code_version": "version"}
        self.assertEqual(remote.metadata["code_labels"], expected)
        stored = json.loads(self.sink.download_file(remote._ensure()["files"]["wandb-metadata.json"]))
        self.assertEqual(stored["code_labels"], expected)
        raw = remote.rawconfig
        self.assertEqual(raw["_wandb"]["t"], {"12": "other-value", "9": {"1": "script", "2": "repository", "3": "version"}})
        self.assertEqual(raw["_wandb"]["code_path"], "existing.py")
        self.assertEqual(raw["_wandb"]["pinned_keys"], ["learning_rate"])
        self.assertEqual(remote.config, {"learning_rate": .1})
        raw["_wandb"]["t"]["9"]["1"] = "changed"
        self.assertEqual(remote.rawconfig["_wandb"]["t"]["9"]["1"], "script")
        self.assertEqual(remote._ensure()["config"], config)
        self.assertEqual(json.loads(remote.json_config)["_wandb"]["value"], remote.rawconfig["_wandb"])

    def test_resume_replaces_then_clears_process_labels_preserving_other_config(self):
        run = self.new(id="resume-labels", config={"_wandb": {"t": {"12": "keep", "9": {"3": "stale"}}}})
        run.finish()
        for doc, disabled, expected in [("@wandbcode{new}", False, {"1": "new"}),
                                        ("no marker", False, None), ("@wandbcode{ignored}", True, None)]:
            with self.subTest(doc=doc, disabled=disabled), patch.object(sys.modules["__main__"], "__doc__", doc):
                run = self.new(id="resume-labels", resume="must", settings={"label_disable": disabled})
                run.finish(); remote = self.public(run)
                self.assertEqual(remote.rawconfig["_wandb"]["t"].get("9"), expected)
                self.assertEqual(remote.rawconfig["_wandb"]["t"]["12"], "keep")
                self.assertEqual(remote.metadata["code_labels"], {"code": "new"} if expected else {})

    def test_run_start_empty_state_clears_previous_labels_without_environment_event(self):
        from backprop.apis.public.replay import reduce_events
        run = self.new(id="start-only"); run.finish()
        with patch.object(sys.modules["__main__"], "__doc__", "no marker"):
            run = self.new(id="start-only", resume="must"); run.finish()
        starts = [event for event in self.sink.events if event["type"] == "run_start"]
        state = reduce_events(starts)
        self.assertEqual(state["metadata"]["code_labels"], {})
        self.assertNotIn("_wandb", project_rawconfig(state["config"], state["metadata"]))

    def test_offline_sync_preserves_labels_and_disabled_creates_nothing(self):
        from backprop.transport import sync
        run = self.new(mode="offline", settings={"label_disable": False}); run.finish()
        sync(run.sync_dir, sink=self.sink)
        self.assertEqual(self.public(run).rawconfig["_wandb"]["t"]["9"]["1"], "script")
        before = set(Path(self.temp.name).rglob("*"))
        with patch("backprop.notebook_discovery._shell", side_effect=AssertionError("disabled must not probe")):
            disabled = self.new(mode="disabled"); disabled.finish()
        self.assertEqual(set(Path(self.temp.name).rglob("*")), before)

    def test_notebook_first_cell_wins_without_code_capture(self):
        path = Path(self.temp.name) / "labels.ipynb"
        path.write_text(json.dumps(notebook(['@wandbcode{notebook, r=notebook-repo}'], ['@wandbcode{ignored}'])))
        self.shell(path)
        for disable_code in (False, True):
            with self.subTest(disable_code=disable_code):
                run = self.new(settings={"save_code": False, "disable_code": disable_code}); run.finish()
                self.assertEqual(self.public(run).metadata["code_labels"], {"code": "notebook", "repo": "notebook_repo"})
                self.assertFalse([event for event in self.sink.events if event["run_id"] == run.id and event["type"] == "artifact"])
                self.assertEqual(run.settings.disable_code, disable_code)

    def test_plain_script_with_explicit_notebook_name_uses_main_docstring(self):
        path = Path(self.temp.name) / "valid-but-not-current.ipynb"
        path.write_text(json.dumps(notebook(["@wandbcode{notebook,r=wrong-source}"])))
        with patch("backprop.code_labels._notebook_labels", side_effect=AssertionError("plain script must not probe notebook labels")):
            run = self.new(settings={"notebook_name": str(path), "save_code": False})
            run.finish()
        self.assertEqual(self.public(run).metadata["code_labels"],
                         {"code": "script", "repo": "repository", "code_version": "version"})

    def test_notebook_no_marker_or_malformed_never_uses_script_or_second_cell(self):
        path = Path(self.temp.name) / "empty.ipynb"; self.shell(path)
        for content in [json.dumps(notebook("no marker", "@wandbcode{second}")), "not json"]:
            path.write_text(content)
            run = self.new(config={"safe": 1}); run.finish()
            remote = self.public(run)
            self.assertEqual(remote.metadata["code_labels"], {})
            self.assertEqual(remote.config, {"safe": 1})

    def test_label_disable_skips_notebook_probe_but_not_other_metadata(self):
        self.shell()
        with patch("backprop.code_labels._notebook_labels", side_effect=AssertionError("must not probe")):
            run = self.new(settings={"label_disable": True}); run.finish()
        self.assertEqual(self.public(run).metadata["code_labels"], {})
        self.assertIn("python", self.public(run).metadata)

    def test_provider_probe_once_on_caller_and_no_source_artifact(self):
        import threading
        self.shell(); calls = []
        def request(name, timeout_sec):
            calls.append((name, timeout_sec, threading.current_thread()))
            return {"ipynb": notebook(["@wandbcode{colab,r=cloud}"])}
        module = SimpleNamespace(_message=SimpleNamespace(blocking_request=request))
        with patch.dict(sys.modules, {"google.colab": module}):
            run = self.new(settings={"save_code": False}); run.finish()
        remote = self.public(run)
        self.assertEqual(remote.rawconfig["_wandb"]["t"]["9"], {"1": "colab", "2": "cloud"})
        self.assertEqual(remote.rawconfig, remote.rawconfig)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "get_ipynb")
        self.assertIs(calls[0][2], threading.main_thread())
        self.assertTrue(0 < calls[0][1] <= 5)
        self.assertFalse([event for event in self.sink.events if event["type"] == "artifact"])

    def test_failed_provider_probe_is_nonfatal_and_redacted(self):
        self.shell()
        token = "private-provider-token"
        def fail(*args, **kwargs): raise ValueError(token)
        module = SimpleNamespace(_message=SimpleNamespace(blocking_request=fail))
        with patch.dict(sys.modules, {"google.colab": module}), patch.dict(os.environ, {"BACKPROP_TOKEN": token}):
            run = self.new(config={"safe": 1}); run.finish()
        self.assertEqual(self.public(run).metadata["code_labels"], {})
        self.assertTrue(any("Code label notebook probe failed (ValueError)" in path.read_text()
                            for path in Path(run.sync_dir).rglob("debug-internal.log")))
        self.assertNotIn(token, repr(self.sink.events))
        self.assertTrue(all(token.encode() not in path.read_bytes() for path in Path(run.sync_dir).rglob("*") if path.is_file()))

    def test_known_credential_marker_is_not_persisted(self):
        token = "private-token-identifier"
        with patch.dict(os.environ, {"FOUNDRY_TOKEN": token}), patch.object(sys.modules["__main__"], "__doc__", f"@wandbcode{{{token},r=safe}}"):
            run = self.new(); run.finish()
        self.assertEqual(self.public(run).metadata["code_labels"], {"repo": "safe"})
        self.assertNotIn(token, repr(self.sink.events))

    def test_legacy_config_without_native_label_state_is_unchanged(self):
        original = {"_wandb": {"t": {"9": {"1": "legacy"}}}}
        self.assertEqual(project_rawconfig(copy.deepcopy(original), {}), original)


if __name__ == "__main__":
    unittest.main()
