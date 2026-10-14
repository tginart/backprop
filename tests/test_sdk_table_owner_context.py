"""Run-owned artifacts supply real context to custom Table dtype codecs.

Architecture: a registered custom type stores its constraint in an artifact file
-> Run.log stages history and its actual run-table artifact together -> offline
sync/public Artifact.get reconstruct the type with its verified owning artifact.
Failure/replacement tests keep schema files and provenance out of committed state
until the containing value is accepted. Native relationship IDs remain separate
from artifact file paths. No network or optional package is needed.
"""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import backprop
from backprop import table_types as types
from backprop.transport import sync


def context_type(download_root, owners):
    """Build a codec that requires an actual Artifact on both directions.

    Main callers: tests. The file is a real constraint dependency, so a fake
    context or dropped schema cannot make validation and readback succeed.
    """
    class FileConstrainedInteger(types.Type):
        name = "backprop_test_run_file_integer"

        def __init__(self, minimum=0):
            self.params["minimum"] = minimum

        def assign(self, value=None):
            return self if type(value) is int and value >= self.params["minimum"] else types.InvalidType()

        def to_json(self, artifact=None):
            if not isinstance(artifact, backprop.Artifact):
                raise ValueError("A real owning artifact is required")
            owners.append(artifact)
            path = f"constraints/min-{self.params['minimum']}.json"
            if path not in artifact.manifest.entries:
                with artifact.new_file(path, mode="w") as stream:
                    json.dump(self.params, stream)
            return {"wb_type": self.name, "params": {"constraint_file": path}}

        @classmethod
        def from_json(cls, value, artifact=None):
            if not isinstance(artifact, backprop.Artifact):
                raise ValueError("A real source artifact is required")
            entry = artifact.get_entry(value["params"]["constraint_file"])
            path = entry.download(root=download_root)
            return cls(**json.loads(Path(path).read_text()))
    return FileConstrainedInteger


class TableOwnerContextTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.owners = []
        self.handler = context_type(self.root / "constraint-downloads", self.owners)
        types.TypeRegistry.add(self.handler)
        self.sink = backprop.MemorySink()
        self.run = backprop.Run(project="table-owner-test", dir=self.root, mode="offline",
                                settings={"silent": True, "console": "off", "disable_git": True})

    def tearDown(self):
        if not self.run._closed:
            self.run.finish()
        types.TypeRegistry.types_by_name().pop(self.handler.name, None)
        self.temporary.cleanup()

    def table(self, mode="IMMUTABLE", value=3):
        return backprop.Table(columns=["value"], data=[[value]], dtype=self.handler(2),
                              optional=False, log_mode=mode)

    def reader(self):
        self.run.finish()
        sync(self.run.sync_dir, sink=self.sink)
        return backprop.Api._from_store(self.sink, project=self.run.project).run(self.run.id)

    def test_history_summary_and_immutable_reuse_have_real_owner_and_full_constraints(self):
        table = self.table()
        self.run.log({"table": table})
        self.run.log({"table": table})
        self.run.summary["summary_table"] = self.table(value=5)
        reader = self.reader()
        artifacts = list(reader.logged_artifacts())
        self.assertEqual(len(artifacts), 2)
        self.assertTrue(self.owners)
        for artifact in artifacts:
            self.assertIn("constraints/min-2.json", artifact.manifest.entries)
            entry = next(entry for entry in artifact.manifest.entries.values() if entry.path.endswith(".table.json"))
            restored = artifact.get(entry.path)
            self.assertEqual(restored._types[0]._runtime_type.params["minimum"], 2)
            restored.add_data(6)
            with self.assertRaises(TypeError):
                restored.add_data(1)
        rows = list(reader.scan_history())
        self.assertEqual(rows[0]["table"]["artifact_path"], rows[1]["table"]["artifact_path"])
        self.assertEqual(len({id(owner) for owner in self.owners}), 2)

    def test_invalid_late_value_and_replaced_pending_key_publish_no_artifact(self):
        table = self.table()
        before = self.run._journal.count
        with self.assertRaises(TypeError):
            self.run.log({"table": table, "invalid": object()})
        self.assertEqual(self.run._journal.count, before)
        self.assertEqual(table._offset, 0)
        self.assertIsNone(table._artifact_target)
        self.run.log({"table": table}, commit=False)
        self.run.log({"table": 1})
        reader = self.reader()
        self.assertEqual(list(reader.logged_artifacts()), [])
        self.assertEqual(list(reader.scan_history())[0]["table"], 1)
        self.assertEqual(table._offset, 0)
        self.assertIsNone(table._artifact_target)

    def test_mutable_and_incremental_versions_keep_each_owner_schema(self):
        mutable, incremental = self.table("MUTABLE"), self.table("INCREMENTAL")
        self.run.log({"mutable": mutable, "incremental": incremental})
        mutable.add_data(4)
        incremental.add_data(4)
        self.run.log({"mutable": mutable, "incremental": incremental})
        reader = self.reader()
        artifacts = list(reader.logged_artifacts())
        self.assertEqual(len(artifacts), 4)
        for artifact in artifacts:
            table_entry = next(entry for entry in artifact.manifest.entries.values() if entry.path.endswith(".table.json"))
            result = artifact.get(table_entry.path)
            self.assertIn(result.data, ([[3]], [[3], [4]]))
            self.assertEqual(result._types[0]._runtime_type.params["minimum"], 2)
            with self.assertRaises(TypeError):
                result.add_data(1)
        rows = list(reader.scan_history())
        self.assertEqual([row["incremental"]["row_offset"] for row in rows], [0, 1])
        self.assertNotEqual(rows[0]["incremental"]["artifact_path"], rows[1]["incremental"]["artifact_path"])

    def test_resumed_incremental_schema_reads_its_actual_prior_artifact(self):
        self.run.log({"table": self.table("INCREMENTAL")})
        prior = self.reader()
        identity = self.run.id
        self.run = backprop.Run(project=prior.project, id=identity, resume="must", dir=self.root,
                                _sink=self.sink, _deployment={},
                                settings={"silent": True, "console": "off", "disable_git": True})
        continuation = self.table("INCREMENTAL", value=4)
        self.run.log({"table": continuation})
        self.run.finish()
        reader = backprop.Api._from_store(self.sink, project=prior.project).run(identity)
        rows = list(reader.scan_history())
        self.assertEqual([row["table"]["row_offset"] for row in rows], [0, 1])
        self.assertEqual([row["table"]["data"] for row in rows], [[[3]], [[4]]])
        artifact = list(reader.logged_artifacts())[-1]
        entry = next(item for item in artifact.manifest.entries.values() if item.path.endswith(".table.json"))
        restored = artifact.get(entry.path)
        self.assertEqual(restored.data, [[3], [4]])
        with self.assertRaises(TypeError):
            restored.add_data(1)

    def test_resumed_owner_lookup_failure_is_atomic_and_can_retry(self):
        self.run.log({"table": self.table("INCREMENTAL")})
        self.reader()
        self.run = backprop.Run(project=self.run.project, id=self.run.id, resume="must", dir=self.root,
                                _sink=self.sink, _deployment={},
                                settings={"silent": True, "console": "off", "disable_git": True})
        continuation = self.table("INCREMENTAL", value=4)
        before = (self.run._journal.count, continuation._id, continuation._offset)
        with patch("backprop.artifact_references.native_reference_entry", side_effect=ValueError("source unavailable")):
            with self.assertRaisesRegex(ValueError, "source unavailable"):
                self.run.log({"table": continuation})
        self.assertEqual((self.run._journal.count, continuation._id, continuation._offset), before)
        self.assertIsNone(continuation._snapshot)
        self.assertIsNone(continuation._artifact_target)
        self.run.log({"table": continuation})
        self.run.finish()
        reader = backprop.Api._from_store(self.sink, project=self.run.project).run(self.run.id)
        self.assertEqual(len(list(reader.scan_history())), 2)
        self.assertEqual(len(list(reader.logged_artifacts())), 2)

    def test_nested_custom_schema_keeps_history_relationship_ids(self):
        parent = backprop.Table(columns=["label"], data=[["parent"]])
        custom = types.TypedDictType({"nested": types.ListType(types.OptionalType(self.handler(2)))})
        table = backprop.Table(columns=["custom", "parent"], data=[[{"nested": [3, None]}, parent.index_ref(0)]],
                               dtype=[custom, parent.index_ref(0)], optional=False)
        self.run.log({"linked": table})
        reader = self.reader()
        descriptor = list(reader.scan_history())[0]["linked"]
        self.assertEqual(descriptor["column_types"][1]["table_id"], parent._id)
        self.assertNotIn("table_path", descriptor["column_types"][1])
        self.assertIn(parent._id, descriptor["related_tables"])
        artifact = list(reader.logged_artifacts())[0]
        restored = artifact.get("linked")
        self.assertEqual(restored.data[0][0], {"nested": [3, None]})
        self.assertEqual(restored.data[0][1].get_row(), {"label": "parent"})
        with self.assertRaises(TypeError):
            restored.add_data({"nested": [1]}, restored.data[0][1])


if __name__ == "__main__":
    unittest.main()
