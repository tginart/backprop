"""Produce shared partition/join inputs and Python results from actual artifacts.

Architecture: real SDK construction -> MemorySink event/file persistence ->
public artifact readback -> browser-consumable manifest and JSON snapshots.
Main callers: Python tests, Node replay tests and browser table QA.
"""
import json
import tempfile
import backprop as w
from backprop.data_types import PartitionedTable
from backprop.apis.public import Api


def fixture():
    with tempfile.TemporaryDirectory() as directory:
        store=w.MemorySink()
        run=w.init(project="partition",entity="e",dir=directory,_sink=store,settings={"console":"off","silent":True,"x_disable_stats":True})
        artifact=w.Artifact("partitions","dataset")
        artifact.add(w.Table(columns=["id","left"],data=[],allow_mixed_types=True),"left/empty")
        artifact.add(w.Table(columns=["id","left"],data=[[1,"b"],[1,"a"]],allow_mixed_types=True),"left/b")
        artifact.add(w.Table(columns=["id","left"],data=[[None,"null"],[2,"unmatched"]],allow_mixed_types=True),"left/a")
        artifact.add(w.Table(columns=["id","left"],data=[[99,"exclude"]]),"left-extra/a")
        artifact.add(w.Table(columns=["id","right"],data=[[1,"x"],[1,"y"],[3,"orphan"],[None,"null"]],allow_mixed_types=True),"right/rows")
        left=PartitionedTable("left");right=PartitionedTable("right")
        artifact.add(left,"left-table");artifact.add(right,"right-table")
        joined=w.JoinedTable(left,right,"id");artifact.add(joined,"joined")
        run.log_artifact(artifact).wait();run.log({"parts":left,"joined":joined});run.finish()
        api=Api._from_store(store,entity="e",project="partition")
        saved=api.artifact(artifact.id)
        payload=next(event["payload"] for event in store.events if event["type"]=="artifact")
        values={entry.path:json.loads(entry._read_verified_bytes()) for entry in saved.manifest.entries.values() if entry.path.endswith(".json")}
        materialized=saved.get("joined").materialize()
        partition=saved.get("left-table")._materialize()
        snapshot=next(event["payload"]["metrics"]["parts"] for event in store.events if event["type"]=="history")
        return {"partition_info":partition._partition_info,"snapshot":snapshot,"files":payload["files"],"json":values,"rows":[list(row) for _,row in saved.get("left-table").iterrows()],
                "joined":{"_type":"table","columns":materialized.columns,"data":materialized.data}}

if __name__=="__main__":print(json.dumps(fixture()))
