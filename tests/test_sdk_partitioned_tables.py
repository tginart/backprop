"""Partition contracts against pinned source and persisted artifact readback.

Architecture: unchanged upstream class -> the same fake manifest fixtures ->
observable comparison; real Backprop artifacts verify ownership and serialization.
Main callers: SDK CI. Optional libraries and external services are unnecessary.
"""
import ast
import hashlib
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import backprop as w
from backprop.data_types import PartitionedTable
from backprop.values import WBValue
from partition_replay_fixture import fixture

ROOT=Path(__file__).resolve().parents[1]
SOURCE=ROOT/".reference/wandb/source/wandb/sdk/data_types/table.py"


def reference_class():
    source=SOURCE.read_bytes()
    # Fail if the declared baseline was silently changed.
    assert hashlib.sha256(source).hexdigest()=="652cc8b4a9d51ef78917a5ea213ac5ccb9af4fce704a2293c812a01d9aaa96bd"
    tree=ast.parse(source)
    nodes=[node for node in tree.body if isinstance(node,ast.ClassDef) and node.name in {"PartitionedTable","_PartitionTablePartEntry"}]
    namespace={"Media":WBValue,"wandb":SimpleNamespace(Run=w.Run)}
    exec(compile(ast.Module(body=nodes,type_ignores=[]),str(SOURCE),"exec"),namespace)
    return namespace["PartitionedTable"]


class PartitionedTables(unittest.TestCase):
    def test_constructor_roundtrip_and_iteration_match_pinned_source(self):
        if not SOURCE.exists():self.skipTest("Pinned source unavailable")
        reference=reference_class()
        tables={"parts/b.table.json":w.Table(columns=["id","value"],data=[[2,"b"],[1,"a"]]),
                "parts/a.table.json":w.Table(columns=["id","value"],data=[]),
                "parts/c.table.json":w.Table(columns=["id","value"],data=[[3,"c"]])}
        entries=[SimpleNamespace(path=path) for path in tables]
        source=SimpleNamespace(manifest=SimpleNamespace(get_entries_in_directory=lambda path:entries),get=tables.get)
        for cls in (reference,PartitionedTable):
            raw=cls("parts")
            self.assertEqual(list(raw.iterrows()),[])
            self.assertEqual(raw.to_json(None),{"_type":"partitioned-table","parts_path":"parts"})
            value=cls.from_json(raw.to_json(None),source)
            self.assertEqual(list(value.iterrows()),[(0,[2,"b"]),(1,[1,"a"]),(2,[3,"c"])])
            self.assertEqual(list(value.iterrows()),[(0,[2,"b"]),(1,[1,"a"]),(2,[3,"c"])])
            self.assertTrue(all(entry._part is None for entry in value._loaded_part_entries.values()))
            self.assertEqual(value,cls("parts"));self.assertNotEqual(value,cls("other"))
            with self.assertRaisesRegex(ValueError,"cannot be bound"):value.bind_to_run(None,"x",0)
        tables["parts/c.table.json"]=w.Table(columns=["different"],data=[[1]])
        for cls in (reference,PartitionedTable):
            with self.assertRaisesRegex(ValueError,"non-matching columns"):
                list(cls.from_json({"parts_path":"parts"},source).iterrows())

    def test_artifact_and_join_replay_uses_all_parts_and_duplicate_keys(self):
        result=fixture()
        self.assertEqual(result["rows"],[[1,"b"],[1,"a"],[None,"null"],[2,"unmatched"]])
        self.assertEqual(result["joined"]["data"],[[1,"b",1,"x"],[1,"b",1,"y"],[1,"a",1,"x"],[1,"a",1,"y"],[None,"null",None,"null"],[2,"unmatched",None,None],[None,None,3,"orphan"]])

    def test_aliases_registry_and_readding_partition_copy_dependencies(self):
        from backprop.compatibility import install_wandb_aliases
        install_wandb_aliases()
        from wandb.data_types import PartitionedTable as Public
        from wandb.sdk.data_types.table import PartitionedTable as Internal
        self.assertIs(Public,PartitionedTable);self.assertIs(Internal,PartitionedTable)
        artifact=w.Artifact("original","dataset")
        artifact.add(w.Table(columns=["text"],data=[[w.Html("<p>Hello</p>",inject=False)]]),"parts/one")
        artifact.add(PartitionedTable("parts"),"partition")
        original=artifact.get("partition")
        other=w.Artifact("copy","dataset");other.add(original,"copied")
        rows=list(other.get("copied").iterrows())
        self.assertEqual(rows[0][1][0]._bytes(),b"<p>Hello</p>")
        self.assertIs(WBValue.type_mapping()["partitioned-table"],PartitionedTable)
        restored=WBValue.init_from_json(original.to_json(None),artifact)
        self.assertIsInstance(restored,PartitionedTable)

    def test_empty_invalid_and_repeated_partitions(self):
        artifact=w.Artifact("empty","dataset");artifact.add(PartitionedTable("empty"),"partition")
        value=artifact.get("partition")
        self.assertEqual(list(value.iterrows()),[])
        self.assertEqual(value._materialize().data,[])
        self.assertEqual(value._materialize().columns,[])
        artifact.add(w.Table(columns=["a"],data=[[1]]),"bad/a")
        artifact.add(w.Table(columns=["b"],data=[[2]]),"bad/b")
        artifact.add(PartitionedTable("bad"),"mismatch")
        with self.assertRaisesRegex(ValueError,"non-matching columns"):artifact.get("mismatch")._materialize()
        with self.assertRaises(ValueError):PartitionedTable("parts")._materialize()
        with self.assertRaises(TypeError):PartitionedTable.from_json({"parts_path":[]},artifact)
        with self.assertRaises(NotImplementedError):PartitionedTable.get_media_subdir()
        self.assertFalse(value.file_is_set());self.assertFalse(value.is_bound())
        with self.assertRaises(NotImplementedError):value.to_data_array()

    def test_copy_conflicts_are_atomic_and_retry_preserves_existing_files(self):
        original=w.Artifact("source-parts","dataset")
        original.add(w.Table(columns=["x"],data=[[1]]),"parts/a")
        original.add(w.Table(columns=["x"],data=[[2]]),"parts/b")
        original.add(PartitionedTable("parts"),"all")
        copied=w.Artifact("copy-parts","dataset")
        conflicting=copied.add(w.Table(columns=["x"],data=[[99]]),"parts/b")
        before=dict(copied.manifest.entries)
        with self.assertRaisesRegex(ValueError,"Conflicting partition"):
            copied.add(original.get("all"),"copy")
        self.assertEqual(copied.manifest.entries,before)
        self.assertEqual(copied.get_entry(conflicting.path)._read_verified_bytes(),conflicting._read_verified_bytes())
        copied.add(w.Table(columns=["x"],data=[[2]]),"parts/b",overwrite=True)
        copied.add(original.get("all"),"copy")
        self.assertEqual([row for _,row in copied.get("copy").iterrows()],[[1],[2]])

    def test_upload_retry_after_restart_uses_durable_partition_files(self):
        from backprop.transport import sync
        from backprop.apis.public import Api
        class Ambiguous(w.MemorySink):
            def publish(self,events):super().publish(events);raise OSError("response lost")
        with tempfile.TemporaryDirectory() as directory:
            run=w.Run(project="partition-recovery",entity="e",dir=directory,mode="offline",
                      settings={"console":"off","silent":True,"x_disable_stats":True})
            artifact=w.Artifact("parts-retry","dataset")
            artifact.add(w.Table(columns=["x"],data=[[1]]),"parts/one")
            parts=PartitionedTable("parts");artifact.add(parts,"all")
            run.log_artifact(artifact);run.log({"parts":parts});run.finish()
            first=Ambiguous()
            with self.assertRaises(Exception):sync(run.sync_dir,sink=first)
            second=w.MemorySink();sync(run.sync_dir,sink=second)
            self.assertEqual(first.events,second.events)
            self.assertEqual(first.files,second.files)
            # Replay duplicate and reordered delivery, then inspect exact parts.
            second.events[:]=list(reversed(second.events))+second.events
            restored=Api._from_store(second,entity="e",project="partition-recovery").artifact(artifact.id)
            self.assertEqual(list(restored.get("all").iterrows()),[(0,[1])])

    def test_joined_constructor_and_missing_reference_decode_match_source(self):
        from backprop.media_base import Media
        tree=ast.parse(SOURCE.read_text())
        node=next(node for node in tree.body if isinstance(node,ast.ClassDef) and node.name=="JoinedTable")
        namespace={"Media":Media,"Table":w.Table,"PartitionedTable":PartitionedTable,"wandb":w}
        exec(compile(ast.Module(body=[node],type_ignores=[]),str(SOURCE),"exec"),namespace)
        reference=namespace["JoinedTable"]
        table=w.Table(columns=["id"],data=[[1]])
        inputs=[table,PartitionedTable("parts"),"x.table.json","x.partitioned-table.json",None,
                SimpleNamespace(ref_url=lambda:"backprop-artifact://abc/x.table.json")]
        keys=["id",["id","id"],[0,1],[],("id","id"),None]
        for value in inputs:
            for key in keys:
                outcomes=[]
                for cls in (reference,w.JoinedTable):
                    try:
                        obj=cls(value,table,key)
                        outcomes.append((obj._table1 is value,obj._table2 is table,obj._join_key))
                    except Exception as error:outcomes.append(type(error).__name__)
                self.assertEqual(outcomes[0],outcomes[1])
        empty=SimpleNamespace(get=lambda path:None)
        for cls in (reference,w.JoinedTable):
            decoded=cls.from_json({"table1":"a.table.json","table2":"b.table.json","join_key":"id"},empty)
            self.assertEqual((decoded._table1,decoded._table2),("a.table.json","b.table.json"))
        with self.assertRaisesRegex(ValueError,"not found"):
            w.JoinedTable("missing.table.json",table,"id").to_json(w.Artifact("missing","dataset"))

    def test_partition_inherited_contracts_use_media_state(self):
        value=PartitionedTable("parts")
        self.assertFalse(value.captions([value]));value._caption="caption"
        self.assertEqual(value.captions([value,PartitionedTable("other")]),["caption",None])
        with self.assertRaises(IndexError):value.captions([])
        self.assertFalse(value.file_is_set());self.assertFalse(value.is_bound())
        for call in (value.to_data_array,value.get_media_subdir):
            with self.assertRaises(NotImplementedError):call()
        self.assertTrue(value.path_is_reference("s3://bucket/part"))
        self.assertFalse(value.path_is_reference(Path("s3://bucket/part")))
        self.assertEqual(value.with_suffix("x"),"x.partitioned-table.json")
        self.assertEqual(value.with_suffix("x.partitioned-table.json"),"x.partitioned-table.json")

    def test_copy_rejects_unrelated_parts_and_keeps_legacy_evaluation_roles(self):
        source=w.Artifact("old-evaluation","dataset")
        table=w.EvalTable(columns=["input","score"],data=[["x",1]],input_columns=["input"],score_columns=["score"])
        with source.new_file("parts/legacy.table.json",encoding="utf-8") as output:
            json.dump(table.to_json(source),output)
        source.add(PartitionedTable("parts"),"all")
        destination=w.Artifact("evaluation-copy","dataset")
        destination.add(source.get("all"),"copied")
        restored=destination.get("copied")._materialize()
        self.assertEqual(restored.data,[["x",1]])
        self.assertEqual(restored._partition_info["partition_parts"][0]["evaluation_roles"]["score_columns"],["score"])
        conflict=w.Artifact("extra-parts","dataset")
        conflict.add(w.Table(columns=["input","score"],data=[["extra",2]]),"parts/extra")
        before=dict(conflict.manifest.entries)
        with self.assertRaisesRegex(ValueError,"Conflicting partition"):
            conflict.add(source.get("all"),"copied")
        self.assertEqual(conflict.manifest.entries,before)
