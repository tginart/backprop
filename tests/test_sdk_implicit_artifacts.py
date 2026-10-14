"""Automatic table artifacts are part of the same durable logging transaction.

Architecture: log typed values -> inspect embedded native manifests -> recover
through public artifact readers. Tests distinguish mutable versions, incremental
delta entries, legacy offline views, rejected rows and pandas normalization.
Main callers: unittest discovery and release verification.
"""
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sdk"))
import backprop
from backprop.implicit_artifacts import artifact_name
from backprop.offline_artifacts import artifact_reference
from backprop.transport import MemorySink, read_journal, sync


class ImplicitTableArtifactContracts(unittest.TestCase):
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

    def new(self, *, id="auto", offline=False, allow=True, **kwargs):
        run = backprop.Run(project="implicit", entity="tests", id=id, dir=kwargs.pop("dir", self.root),
            mode="offline" if offline else "online", settings={"console": "off", "silent": True,
            "disable_git": True, "allow_offline_artifacts": allow, "flush_interval": 3600},
            _sink=None if offline else self.store, _deployment={}, **kwargs)
        self.runs.append(run)
        return run

    def history(self, run):
        return [event for event in read_journal(run.sync_dir) if event["type"] == "history"]

    def artifacts(self, run):
        api = backprop.Api._from_store(self.store, entity=run.entity, project=run.project)
        return list(api.run(run.id).logged_artifacts())

    def test_automatic_table_is_retrievable_by_public_run_and_exact_immutable_name(self):
        run = self.new()
        run.log({"samples": backprop.Table(columns=["x"], data=[[1], [2]])})
        event = self.history(run)[0]
        self.assertEqual(len(event["payload"]["artifacts"]), 1)
        manifest = event["payload"]["artifacts"][0]
        self.assertEqual((manifest["name"], manifest["artifact_type"]), ("run-auto-samples", "run_table"))
        self.assertIn(manifest["artifact_id"], event["payload"]["metrics"]["samples"]["artifact_path"])
        run.finish()
        artifacts = self.artifacts(run)
        self.assertEqual(len(artifacts), 1)
        self.assertEqual(artifacts[0].get("samples").data, [[1], [2]])
        api = backprop.Api._from_store(self.store, entity="tests", project="implicit")
        self.assertEqual(api.artifact(artifacts[0].qualified_name).id, artifacts[0].id)

    def test_offline_false_retains_full_automatic_artifact_and_legacy_visible_view(self):
        run = self.new(offline=True, allow=False)
        with patch.object(backprop.Table, "MAX_ROWS", 2):
            run.log({"samples": backprop.Table(columns=["x"], data=[[n] for n in range(4)])})
        event = self.history(run)[0]
        self.assertEqual(len(event["payload"]["metrics"]["samples"]["data"]), 2)
        self.assertNotIn("artifact_path", event["payload"]["metrics"]["samples"])
        self.assertEqual(len(event["payload"]["artifacts"]), 1)
        run.finish()
        sync(run.sync_dir, sink=self.store)
        self.assertEqual(self.artifacts(run)[0].get("samples").data, [[0], [1], [2], [3]])

    def test_mutable_versions_and_immutable_relog_have_correct_identity(self):
        run = self.new()
        mutable = backprop.Table(columns=["x"], data=[[1]], log_mode="MUTABLE")
        run.log({"changing": mutable})
        mutable.add_data(2)
        run.log({"changing": mutable})
        immutable = backprop.Table(columns=["x"], data=[[3]])
        run.log({"fixed": immutable})
        run.log({"fixed": immutable})
        events = self.history(run)
        first, second = [event["payload"]["artifacts"][0] for event in events[:2]]
        self.assertNotEqual(first["artifact_id"], second["artifact_id"])
        self.assertNotEqual(first["version"], second["version"])
        self.assertIn(second["artifact_id"], events[1]["payload"]["metrics"]["changing"]["artifact_path"])
        self.assertNotIn("artifacts", events[-1]["payload"])
        run.finish()
        artifacts = self.artifacts(run)
        self.assertEqual(len(artifacts), 3)
        versions = [artifact.get("changing").data for artifact in artifacts if artifact.type == "run_table" and artifact.get("changing")]
        self.assertEqual(versions, [[[1]], [[1], [2]]])

    def test_same_instance_top_level_nested_and_list_reuses_one_bound_artifact(self):
        for mode in ("IMMUTABLE", "MUTABLE", "INCREMENTAL"):
            with self.subTest(mode=mode):
                run = self.new(id=mode)
                table = backprop.Table(columns=["x"], data=[[1]], log_mode=mode)
                run.log({"first": table, "second": table, "list": [table, table], "nested": {"third": table}})
                payload = self.history(run)[0]["payload"]
                self.assertEqual(len(payload["artifacts"]), 1)
                identity = payload["artifacts"][0]["artifact_id"]
                values = payload["metrics"]
                for value in [values["first"], values["second"], *values["list"], values["nested"]["third"]]:
                    self.assertIn(identity, value["artifact_path"])
                    self.assertEqual(value["data"], [[1]])
                run.log({"unchanged": table})
                again = self.history(run)[-1]["payload"]
                self.assertNotIn("artifacts", again)
                self.assertIn(identity, again["metrics"]["unchanged"]["artifact_path"])
                self.assertEqual(again["metrics"]["unchanged"]["data"], [[1]])
                if mode != "IMMUTABLE":
                    table.add_data(2)
                    run.log({"changed": table, "also": table})
                    changed = self.history(run)[-1]["payload"]
                    self.assertEqual(len(changed["artifacts"]), 1)
                    self.assertNotEqual(changed["artifacts"][0]["artifact_id"], identity)
                    self.assertEqual(changed["metrics"]["changed"]["data"], [[2]] if mode == "INCREMENTAL" else [[1], [2]])
                run.finish()

    def test_pending_alias_owns_manifest_after_original_key_is_overwritten(self):
        run = self.new()
        table = backprop.Table(columns=["x"], data=[[1]], log_mode="INCREMENTAL")
        run.log({"original": table}, commit=False)
        identity = run._pending_commits["original"].artifacts[0]["artifact_id"]
        run.log({"alias": table}, commit=False)
        self.assertEqual(run._pending_commits["alias"].artifacts[0]["artifact_id"], identity)
        run.log({"original": 42})
        payload = self.history(run)[0]["payload"]
        self.assertEqual([item["artifact_id"] for item in payload["artifacts"]], [identity])
        self.assertEqual(table._offset, 1)
        self.assertIn(identity, payload["metrics"]["alias"]["artifact_path"])
        run.finish()
        self.assertEqual(len(self.artifacts(run)), 1)

    def test_invalid_repeated_value_does_not_leak_transaction_cache(self):
        run = self.new()
        table = backprop.Table(columns=["x"], data=[[1]])
        count = run._journal.count
        with self.assertRaises(TypeError):
            run.log({"original": table, "nested": {"alias": table}, "bad": object()})
        self.assertEqual(run._journal.count, count)
        self.assertIsNone(table._snapshot)
        self.assertIsNone(getattr(table, "_artifact_target", None))
        self.assertEqual(run._pending_commits, {})
        run.log({"recovered": table})
        manifest = self.history(run)[0]["payload"]["artifacts"][0]
        self.assertEqual(manifest["name"], "run-auto-recovered")

    def test_incremental_artifacts_store_deltas_and_resume_preserves_reference_chain(self):
        run = self.new()
        table = backprop.Table(columns=["x"], data=[[0], [1]], log_mode="INCREMENTAL")
        run.log({"samples": table})
        table.add_data(2)
        run.log({"samples": table})
        self.assertTrue(all(not getattr(value, "_implicit_artifacts", {})
                            for value, _entry in table._artifact_target._objects.values()))
        run.finish()
        first_events = self.history(run)
        first_descriptor = first_events[0]["payload"]["metrics"]["samples"]
        second_descriptor = first_events[1]["payload"]["metrics"]["samples"]
        self.assertEqual(second_descriptor["previous_increments_paths"], [first_descriptor["artifact_path"]])
        self.assertEqual(second_descriptor["increment_num"], 1)
        resumed = self.new(resume="must", dir=self.root / "resumed")
        resumed.log({"samples": backprop.Table(columns=["x"], data=[[3]], log_mode="INCREMENTAL")})
        latest = self.history(resumed)[-1]["payload"]
        self.assertEqual((latest["metrics"]["samples"]["row_offset"], latest["metrics"]["samples"]["increment_num"]), (3, 2))
        self.assertEqual(latest["metrics"]["samples"]["previous_increments_paths"],
                         [first_descriptor["artifact_path"], second_descriptor["artifact_path"]])
        self.assertEqual(latest["artifacts"][0]["name"], "run-auto-incr-samples")
        self.assertEqual(latest["artifacts"][0]["artifact_type"], "wandb-run-incremental-table")
        resumed.finish()
        rows = []
        for number, artifact in enumerate(self.artifacts(resumed)):
            names = [name for name in artifact.manifest.entries if name.endswith(".table.json")]
            self.assertEqual(len(names), 1)
            recovered = artifact.get(names[0])
            rows.append(recovered.data)
            self.assertEqual(recovered._increment_num, number)
            self.assertEqual(len(recovered._previous_increments_paths), number)
            raw = json.loads(artifact.get_entry(names[0])._read_verified_bytes())
            self.assertEqual(raw["increment_num"], number)
            self.assertEqual(raw["data"], [[[0], [1]], [[2]], [[3]]][number])
        self.assertEqual(rows, [[[0], [1]], [[0], [1], [2]], [[0], [1], [2], [3]]])

    def test_invalid_or_overwritten_pending_values_do_not_publish_or_mutate_provenance(self):
        run = self.new()
        table = backprop.Table(columns=["x"], data=[[1]])
        count = run._journal.count
        with self.assertRaises(TypeError):
            run.log({"samples": table, "bad": object()})
        self.assertEqual(run._journal.count, count)
        self.assertIsNone(getattr(table, "_artifact_target", None))
        self.assertEqual(table._offset, 0)
        run.log({"samples": table}, commit=False)
        run.log({"samples": 42})
        self.assertNotIn("artifacts", self.history(run)[0]["payload"])
        self.assertIsNone(getattr(table, "_artifact_target", None))
        run.finish()
        self.assertEqual(self.artifacts(run), [])

    def test_failed_journal_append_can_retry_same_staged_identity(self):
        run = self.new()
        table = backprop.Table(columns=["x"], data=[[1]])
        append = run._journal.append
        with patch.object(run._journal, "append", side_effect=OSError("disk unavailable")):
            with self.assertRaisesRegex(OSError, "disk unavailable"):
                run.log({"samples": table})
        manifest = run._pending_commits["samples"].artifacts[0]
        self.assertEqual(table._offset, 0)
        self.assertIsNone(getattr(table, "_artifact_target", None))
        run._commit()
        self.assertEqual(self.history(run)[0]["payload"]["artifacts"][0]["artifact_id"], manifest["artifact_id"])
        self.assertEqual(table._offset, 1)

    def test_summary_and_custom_chart_use_retrievable_table_names(self):
        run = self.new()
        run.summary["final"] = backprop.Table(columns=["x"], data=[[3]])
        chart = backprop.plot.line(backprop.Table(columns=["x", "y"], data=[[1, 2]]), "x", "y")
        split = backprop.plot.line(backprop.Table(columns=["x", "y"], data=[[2, 3]]), "x", "y", split_table=True)
        run.log({"curve": chart, "split": split})
        run.finish()
        artifacts = self.artifacts(run)
        expected = {artifact_name(f"run-auto-{key}") for key in ("final", "curve_table", "Custom Chart Tables/split_table")}
        self.assertEqual({artifact._name for artifact in artifacts}, expected)
        self.assertEqual(chart.spec.table_key, "curve_table")
        self.assertEqual(split.spec.table_key, "Custom Chart Tables/split_table")
        for artifact in artifacts:
            self.assertEqual(len(artifact.manifest.entries), 1)

    @unittest.skipUnless(importlib.util.find_spec("pandas"), "Optional pandas is unavailable")
    def test_genuine_pandas_and_subclass_normalize_once_for_history_nested_and_summary(self):
        import pandas as pd
        class Frame(pd.DataFrame):
            pass
        run = self.new()
        run.log({"direct": pd.DataFrame({"x": [1, 2]}), "nested": {"frame": Frame({"x": [3, 4]})}})
        run.summary["result"] = pd.DataFrame({"x": [5, 6]})
        run.finish()
        artifacts = {artifact._name: artifact for artifact in self.artifacts(run)}
        self.assertEqual(len(artifacts), 3)
        for key, rows in (("direct", [[1], [2]]), ("frame", [[3], [4]]), ("result", [[5], [6]])):
            self.assertEqual(artifacts[f"run-auto-{key}"].get(key).data, rows)

    def test_failed_nested_artifact_add_does_not_poison_successful_child_provenance(self):
        child = backprop.Table(columns=["i"], data=[[1]])
        a = backprop.Table(columns=["i"], data=[[0]])
        b = backprop.Table(columns=["i"], data=[[a.index_ref(0)]])
        from backprop.table_native_types import _ForeignIndexType
        a.cast("i", _ForeignIndexType(b), optional=False)
        outer = backprop.Table(columns=["child", "cycle"], data=[[child.index_ref(0), a.index_ref(0)]])
        artifact = backprop.Artifact("transaction", "dataset")
        with self.assertRaisesRegex(ValueError, "cycle"):
            artifact.add(outer, "outer")
        self.assertIsNone(getattr(child, "_artifact_target", None))
        artifact.add(child, "good")
        run = self.new()
        run.log_artifact(artifact)
        self.assertIn("good.table.json", artifact_reference(child, run._settings))

    def test_relationship_tables_are_embedded_without_consuming_their_log_cursor(self):
        run = self.new()
        source = backprop.Table(columns=["x"], data=[[7], [8]], log_mode="INCREMENTAL")
        linked = backprop.Table(columns=["reference"], data=[[source.index_ref(1)]])
        run.log({"linked": linked})
        event = self.history(run)[0]
        self.assertEqual(len(event["payload"]["artifacts"]), 1)
        self.assertEqual(source._offset, 0)
        self.assertIsNone(source._snapshot)
        run.finish()
        artifact = self.artifacts(run)[0]
        restored = artifact.get("linked")
        self.assertEqual(restored.data[0][0].get_row(), {"x": 8})

    def test_image_copy_preserves_pending_target_and_committed_source(self):
        image = backprop.Image(b"immutable image bytes")
        artifact = backprop.Artifact("image", "dataset")
        artifact.add(image, "sample")
        run = self.new(offline=True)
        run.log_artifact(artifact)
        copied = backprop.Image(image)
        self.assertEqual(artifact_reference(copied, run._settings), artifact_reference(image, run._settings))
        run.finish()
        sync(run.sync_dir, sink=self.store)
        restored = self.artifacts(run)[0].get("sample")
        copied = backprop.Image(restored)
        self.assertEqual(artifact_reference(copied, run._settings), artifact_reference(restored, run._settings))


if __name__ == "__main__":
    unittest.main()
