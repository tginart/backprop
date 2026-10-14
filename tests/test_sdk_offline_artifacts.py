"""Offline reference policy through durable logging and public recovery.

Architecture: compare the pinned reference gate and legacy table slicing ->
exercise native event snapshots -> sync with an ambiguous response -> read
through the public API. Small patched row limits keep boundary tests fast;
large JSON tests exercise real transport externalization. No credentials needed.
Main callers: unittest discovery and release verification.
"""
import ast
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sdk"))
import backprop
from backprop.offline_artifacts import artifact_reference, pending_references_allowed
from backprop.transport import BackpropError, MemorySink, read_journal, sync

ROOT = Path(__file__).resolve().parents[1]
PINNED = ROOT / ".reference/wandb/source/wandb/sdk/data_types"


class OfflineArtifactContracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = MemorySink()
        self.runs = []
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()

    def tearDown(self):
        for run in self.runs:
            if not run._closed:
                run.finish()
        self.environment.stop()
        self.temp.cleanup()

    def run_for(self, id="offline", *, offline=True, allow=True, **kwargs):
        settings = {"console": "off", "silent": True, "disable_git": True,
                    "allow_offline_artifacts": allow, "flush_interval": 3600}
        settings.update(kwargs.pop("settings", {}))
        run = backprop.Run(project="offline-artifacts", entity="tests", id=id,
            dir=kwargs.pop("dir", self.root), mode="offline" if offline else "online",
            settings=settings, _sink=None if offline else self.store, _deployment={}, **kwargs)
        self.runs.append(run)
        return run

    def metrics(self, run):
        return [event["payload"]["metrics"] for event in read_journal(run.sync_dir)
                if event["type"] == "history"]

    def public(self, run):
        return backprop.Api._from_store(self.store, entity="tests", project=run.project).run(run.id)

    def test_false_caps_legacy_view_and_preserves_full_current_artifact(self):
        with patch.object(backprop.Table, "MAX_ROWS", 2):
            for allow, offline in ((False, True), (True, True), (False, False)):
                with self.subTest(allow=allow, offline=offline):
                    run = self.run_for(f"run-{allow}-{offline}", allow=allow, offline=offline)
                    table = backprop.Table(columns=["x"], data=[[n] for n in range(4)])
                    run.log({"table": table})
                    descriptor = self.metrics(run)[0]["table"]
                    if offline and not allow:
                        self.assertEqual(descriptor["data"], [[0], [1]])
                        self.assertTrue(descriptor["legacy_offline"])
                        self.assertEqual((descriptor["nrows"], descriptor["ncols"]), (4, 1))
                        self.assertEqual(descriptor["artifact_snapshot"]["data"], [[0], [1], [2], [3]])
                    else:
                        self.assertEqual(len(descriptor["data"]), 4)
                        self.assertNotIn("legacy_offline", descriptor)
                    self.assertEqual(table._offset, 4)
                    run.finish()

    def test_incremental_false_keeps_standalone_legacy_views_and_complete_increment(self):
        run = self.run_for(allow=False)
        table = backprop.Table(columns=["x"], data=[[n] for n in range(4)], log_mode="INCREMENTAL")
        with patch.object(backprop.Table, "MAX_ROWS", 2):
            run.log({"table": table})
            for value in range(4, 8):
                table.add_data(value)
            run.log({"table": table})
        first, second = [row["table"] for row in self.metrics(run)]
        self.assertNotEqual(first["table_id"], second["table_id"])
        self.assertEqual((first["row_offset"], second["row_offset"]), (0, 0))
        self.assertEqual(second["data"], [[4], [5]])
        archive = second["artifact_snapshot"]
        self.assertEqual(archive["data"], [[4], [5], [6], [7]])
        self.assertEqual((archive["row_offset"], archive["source_row_offset"], archive["source_log_mode"]),
                         (0, 4, "INCREMENTAL"))
        self.assertEqual((table._offset, second["nrows"]), (8, 8))

    def test_legacy_limit_does_not_raise_but_artifact_limit_still_does(self):
        run = self.run_for(allow=False, settings={"strict": True})
        with patch.object(backprop.Table, "MAX_ROWS", 2), patch.object(backprop.Table, "MAX_ARTIFACT_ROWS", 5):
            run.log({"table": backprop.Table(columns=["x"], data=[[n] for n in range(4)])})
            table = backprop.Table(columns=["x"], data=[[n] for n in range(6)])
            before = run._journal.count
            with self.assertRaisesRegex(ValueError, "exceeds 5"):
                run.log({"too_large": table})
            self.assertEqual((run._journal.count, table._offset, table._snapshot), (before, 0, None))

    def test_invalid_mixed_row_never_consumes_legacy_or_archive_rows(self):
        run = self.run_for(allow=False)
        table = backprop.Table(columns=["x"], data=[[n] for n in range(4)], log_mode="INCREMENTAL")
        before = run._journal.count
        with patch.object(backprop.Table, "MAX_ROWS", 2):
            with self.assertRaises(TypeError):
                run.log({"table": table, "invalid": object()})
            self.assertEqual((run._journal.count, table._offset, table._snapshot), (before, 0, None))
            run.log({"table": table})
        self.assertEqual(self.metrics(run)[0]["table"]["artifact_snapshot"]["data"], [[0], [1], [2], [3]])

    def test_explicit_artifact_pending_references_follow_flag_but_retrieval_is_full(self):
        with patch.object(backprop.Table, "MAX_ROWS", 2):
            for allow in (False, True):
                with self.subTest(allow=allow):
                    run = self.run_for(str(allow), allow=allow)
                    table = backprop.Table(columns=["x"], data=[[n] for n in range(4)])
                    artifact = backprop.Artifact(f"table-{allow}", "dataset")
                    entry = artifact.add(table, "table ?#")
                    self.assertIsNone(artifact_reference(table, run._settings))
                    run.log_artifact(artifact)
                    reference = artifact_reference(table, run._settings)
                    self.assertEqual(reference is not None, allow)
                    self.assertIsNone(artifact.id)
                    run.log({"table": table})
                    descriptor = self.metrics(run)[0]["table"]
                    self.assertEqual("artifact_path" in descriptor, allow)
                    if allow:
                        self.assertIn("table%20%3F%23.table.json", reference)
                    run.finish()
                    sync(run.sync_dir, sink=self.store)
                    remote = list(self.public(run).logged_artifacts())[0]
                    self.assertEqual(remote.id, artifact.id)
                    self.assertEqual(remote.get(entry.path).data, [[0], [1], [2], [3]])

    def test_committed_source_reference_is_not_gated_in_offline_false(self):
        source = self.run_for("source", offline=False)
        artifact = backprop.Artifact("source-table", "dataset")
        artifact.add(backprop.Table(columns=["x"], data=[[n] for n in range(4)]), "table")
        source.log_artifact(artifact)
        source.finish()
        table = list(self.public(source).logged_artifacts())[0].get("table")
        consumer = self.run_for("consumer", allow=False)
        with patch.object(backprop.Table, "MAX_ROWS", 2):
            consumer.log({"table": table})
        descriptor = self.metrics(consumer)[0]["table"]
        self.assertEqual(len(descriptor["data"]), 4)
        self.assertNotIn("legacy_offline", descriptor)
        self.assertIn(artifact.id, descriptor["artifact_path"])

    def test_sync_ambiguous_retry_and_fresh_reader_preserve_archive_and_media(self):
        run = self.run_for(allow=False)
        source = self.root / "hello.html"
        source.write_text("<p>durable bytes</p>")
        table = backprop.Table(columns=["x", "html"], data=[[n, backprop.Html(source, inject=False)] for n in range(4)])
        with patch.object(backprop.Table, "MAX_ROWS", 2):
            run.log({"table": table})
        run.finish()
        source.unlink()
        publish = self.store.publish
        def ambiguous(events):
            publish(events)
            raise OSError("response lost")
        with patch.object(self.store, "publish", side_effect=ambiguous):
            with self.assertRaisesRegex(BackpropError, "response lost"):
                sync(run.sync_dir, sink=self.store)
        first = list(self.store.events)
        sync(run.sync_dir, sink=self.store)
        self.assertEqual(self.store.events[len(first):], first)
        rows = self.public(run).history(pandas=False)
        self.assertEqual(len(rows), 1)
        visible = rows[0]["table"]
        self.assertEqual(len(visible["data"]), 2)
        self.assertEqual(len(visible["artifact_snapshot"]["data"]), 4)
        reference = visible["artifact_snapshot"]["data"][3][1]
        self.assertEqual(self.store.download_file(reference), b"<p>durable bytes</p>")

    def test_external_legacy_descriptor_never_resumes_an_artifact_chain(self):
        old = self.run_for(allow=False)
        table = backprop.Table(columns=["text"], data=[["x" * 140_000]], log_mode="INCREMENTAL")
        old.log({"table": table})
        old.finish()
        sync(old.sync_dir, sink=self.store)
        descriptor = next(event["payload"]["metrics"]["table"] for event in self.store.events if event["type"] == "history")
        self.assertTrue(descriptor["external"])
        self.assertTrue(descriptor["legacy_offline"])
        for remove_outer_flag in (False, True):
            if remove_outer_flag:
                # Old external envelopes without retained metadata still hydrate
                # before applying the guard; the content remains authoritative.
                for event in self.store.events:
                    if event["type"] == "history":
                        event["payload"]["metrics"]["table"].pop("legacy_offline", None)
                        event["payload"]["summary"]["table"].pop("legacy_offline", None)
                    if event["type"] == "summary":
                        event["payload"]["values"]["table"].pop("legacy_offline", None)
            resumed = self.run_for(offline=False, resume="must", dir=self.root / str(remove_outer_flag))
            new = backprop.Table(columns=["text"], data=[["fresh"]], log_mode="INCREMENTAL")
            identity = new._id
            resumed.log({"table": new})
            latest = self.metrics(resumed)[-1]["table"]
            self.assertEqual((latest["table_id"], latest["row_offset"]), (identity, 0))
            self.assertNotEqual(latest["table_id"], descriptor["table_id"])
            # Avoid publishing the first probe into the shared prior state.
            resumed._journal.sink = None
            resumed.finish()

    def test_current_settings_precedence_matches_pinned_reference_gate(self):
        filename = PINNED / "base_types/wb_value.py"
        if not filename.exists():
            self.skipTest("Pinned source fixture unavailable")
        singleton = SimpleNamespace(settings=SimpleNamespace(_offline=True, allow_offline_artifacts=False),
                                    most_recent_active_run=None)
        namespace = {}
        with patch.dict(sys.modules, {"wandb.sdk": SimpleNamespace(wandb_setup=SimpleNamespace(singleton=lambda: singleton))}):
            exec(compile(filename.read_text(), str(filename), "exec"), namespace)
        for mode, allow in (("offline", False), ("offline", True), ("online", False), ("online", True)):
            settings = backprop.Settings(mode=mode, allow_offline_artifacts=allow)
            singleton.settings = settings
            self.assertEqual(pending_references_allowed(settings), namespace["_client_ids_allowed"]())
        active = SimpleNamespace(offline=True, _settings=backprop.Settings(mode="offline", allow_offline_artifacts=False))
        singleton.most_recent_active_run = active
        active._closed = False
        with patch.object(backprop, "_runs", [active]), patch.object(backprop, "_setup_settings", backprop.Settings()):
            self.assertEqual(pending_references_allowed(), namespace["_client_ids_allowed"]())
            self.assertFalse(pending_references_allowed())

    def test_legacy_slicing_matches_pinned_method_without_loading_optional_sdk(self):
        filename = PINNED / "table.py"
        if not filename.exists():
            self.skipTest("Pinned source fixture unavailable")
        cls = next(node for node in ast.parse(filename.read_text()).body if isinstance(node, ast.ClassDef) and node.name == "Table")
        method = next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == "_to_table_json")
        namespace = {"Table": SimpleNamespace(MAX_ROWS=2), "Any": object}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[])), str(filename), "exec"), namespace)
        table = SimpleNamespace(columns=["x"], data=[[n] for n in range(8)], log_mode="INCREMENTAL", _last_logged_idx=3)
        expected = namespace["_to_table_json"](table, warn=False)
        self.assertEqual(expected["data"], [[4], [5]])
        run = self.run_for(allow=False)
        actual = backprop.Table(columns=table.columns, data=table.data, log_mode="INCREMENTAL")
        actual._offset = 4
        with patch.object(backprop.Table, "MAX_ROWS", 2):
            run.log({"table": actual})
        self.assertEqual(self.metrics(run)[0]["table"]["data"], expected["data"])

    def test_environment_and_explicit_settings_apply_before_logging(self):
        with patch.dict(os.environ, {"WANDB_ALLOW_OFFLINE_ARTIFACTS": "false"}):
            from backprop.run import resolve_settings
            self.assertFalse(resolve_settings().allow_offline_artifacts)
            self.assertTrue(resolve_settings(backprop.Settings(allow_offline_artifacts=True)).allow_offline_artifacts)
        with self.assertRaises(backprop.UsageError):
            backprop.Settings(allow_offline_artifacts="invalid")


if __name__ == "__main__":
    unittest.main()
