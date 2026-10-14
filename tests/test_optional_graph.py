"""Compare graph contracts and training capture with genuine W&B 0.30.0.

Architecture: separate reference/native subprocesses -> compare observable JSON;
then exercise persistence, incomplete branches, independent runs and cleanup.
Main callers: optional framework CI and local unittest discovery. No packages
are installed here. Missing optional runtimes are explicit verification skips.
"""
import importlib.util
import importlib.metadata
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import backprop
from backprop.graph import Graph, Node
from backprop.apis.public import Api
from backprop.transport import MemorySink

ROOT=Path(__file__).resolve().parents[1]

@unittest.skipUnless(importlib.util.find_spec("torch"),"Optional PyTorch is required")
class GraphContracts(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        downloads=patch.object(backprop.Artifact,'_default_root',lambda artifact,**kwargs:str(Path(self.tmp.name)/'downloads'/artifact._id))
        downloads.start();self.addCleanup(downloads.stop)
        self.store=MemorySink()
        self.run=backprop.init(project="graph",entity="e",dir=self.tmp.name,_sink=self.store,
                              settings={"console":"off","silent":True,"x_disable_stats":True},reinit="create_new")
        self.addCleanup(self.run.finish)
        self.api=Api._from_store(self.store,entity="e",project="graph")

    def test_pinned_values_and_real_training_match_in_isolated_processes(self):
        reference=ROOT/".reference/wandb/source"
        if not reference.is_dir(): self.skipTest("Pinned W&B source is required")
        try:
            distribution=importlib.metadata.distribution("wandb")
        except importlib.metadata.PackageNotFoundError:
            self.skipTest("W&B 0.30.0 wheel supplies the offline reference service")
        self.assertEqual(distribution.version,"0.30.0")
        for name in ("wandb/sdk/data_types/graph.py","wandb/sdk/wandb_watch.py","wandb/integration/torch/wandb_torch.py"):
            self.assertEqual(hashlib.sha256((reference/name).read_bytes()).hexdigest(),
                             hashlib.sha256(Path(distribution.locate_file(name)).read_bytes()).hexdigest())
        reference=Path(distribution.locate_file(""))
        outcomes=[]
        for source in (reference,ROOT/"sdk"):
            env={**os.environ,"PYTHONPATH":str(source),"WANDB_MODE":"offline","BACKPROP_DIR":self.tmp.name}
            result=subprocess.run([sys.executable,"-B",str(ROOT/"tests/graph_reference_probe.py")],env=env,cwd=self.tmp.name,text=True,capture_output=True,timeout=90)
            if result.returncode and source==reference and "ModuleNotFoundError" in result.stderr:
                self.skipTest("Pinned W&B optional reference dependencies unavailable: "+result.stderr.splitlines()[-1])
            self.assertEqual(result.returncode,0,result.stderr)
            outcomes.append(json.loads(next(line.removeprefix("GRAPH_RESULT=") for line in result.stdout.splitlines() if line.startswith("GRAPH_RESULT="))))
        self.assertEqual(outcomes[0],outcomes[1])

    def test_run_artifact_and_aliases_preserve_graph_contents(self):
        from backprop.compatibility import install_wandb_aliases
        install_wandb_aliases()
        import wandb
        from wandb.sdk.data_types.graph import Graph as AliasGraph, Node as AliasNode, Edge
        self.assertIs(wandb.Graph,Graph);self.assertIs(AliasGraph,Graph);self.assertIs(AliasNode,Node)
        self.assertIs(wandb.data_types.Graph,Graph)
        graph=Graph();a=graph.add_node(id="a",name="Input");b=graph.add_node(id="b",name="Dense")
        self.assertIsInstance(graph.add_edge(a,b),Edge)
        self.assertFalse(graph.file_is_set())
        graph.bind_to_run(self.run,"network",0,id_="model",ignore_copy_err=False)
        self.assertTrue(graph.is_bound());self.assertTrue(graph.file_is_set())
        descriptor=graph.to_json(self.run)
        self.assertEqual(descriptor["_type"],"graph-file")
        self.run.log({"network":graph})
        artifact=backprop.Artifact("network","model");artifact.add(graph,"architecture")
        self.run.log_artifact(artifact).wait();self.run.flush()
        restored=self.api.artifact(artifact.id).get("architecture")
        self.assertEqual(restored._to_graph_json(),graph._to_graph_json())
        descriptor=dict(self.api.run(self.run.id).summary["network"])
        self.assertEqual(json.loads(self.store.files[descriptor["path"]]),graph._to_graph_json())
        from backprop.values import WBValue
        self.assertIs(WBValue.type_mapping()["graph-file"],Graph)

    def test_conditional_modules_remain_incomplete_until_observed(self):
        import torch
        class Conditional(torch.nn.Module):
            def __init__(self):
                super().__init__();self.left=torch.nn.Linear(2,1);self.right=torch.nn.Linear(2,1)
            def forward(self,x,right=False):return self.right(x) if right else self.left(x)
        model=Conditional();self.run.watch(model,log=None,log_graph=True)
        graph=self.run._watcher.graphs[id(model)]
        self.assertFalse(graph.loaded);self.assertEqual(len(graph.nodes),0)
        model(torch.ones(1,2));self.run.flush()
        self.assertEqual(graph.metadata["capture"],{"complete":False,"missing_modules":["right"]})
        self.assertEqual(len(graph.nodes),1)
        model(torch.ones(1,2),right=True);self.run.flush();self.api.flush()
        self.assertTrue(graph.loaded);self.assertEqual(len(graph.nodes),2)
        state=self.api.run(self.run.id)
        self.assertTrue(state.summary["graph_0"]["capture"]["complete"])
        count=len(self.store.events);model(torch.ones(1,2));self.run.flush()
        self.assertEqual(len(self.store.events),count)

    def test_capture_stays_with_owner_and_cleanup_allows_new_training(self):
        import torch
        model=torch.nn.Sequential(torch.nn.Linear(2,1))
        self.run.watch(model,log="all",log_freq=1,idx=4,log_graph=True)
        second=backprop.init(project="graph",entity="e",dir=self.tmp.name,_sink=self.store,reinit="create_new",settings={"console":"off","silent":True,"x_disable_stats":True})
        self.addCleanup(second.finish)
        model(torch.ones(1,2)).sum().backward();self.run.log({"loss":1});self.run.flush();second.flush()
        self.assertIn("graph_4",self.api.run(self.run.id).summary)
        self.assertNotIn("graph_4",self.api.run(second.id).summary)
        self.run.unwatch(model)
        self.assertTrue(all(not module._forward_hooks for module in model.modules()))
        self.assertTrue(all(not p._backward_hooks for p in model.parameters()))
        second.watch(model,log_graph=True);second.finish()
        self.assertTrue(all(not module._forward_hooks for module in model.modules()))

    def test_gradient_frequency_counts_backwards_and_public_return_is_none(self):
        import torch
        model=torch.nn.Linear(2,1)
        self.assertIsNone(self.run.watch(model,log="gradients",log_freq=2,idx=0))
        output=model(torch.ones(1,2)).sum()
        output.backward(retain_graph=True);self.run.log({"loss":1})
        output.backward();self.run.log({"loss":2});self.run.flush()
        rows=list(self.api.run(self.run.id).scan_history())
        self.assertNotIn("gradients/weight",rows[0]);self.assertIn("gradients/weight",rows[1])
        with self.assertRaises(TypeError):self.run.watch(object())
        with self.assertRaises(ValueError):self.run.watch(model,log="invalid")



@unittest.skipUnless(importlib.util.find_spec("tensorflow"), "Optional TensorFlow/Keras 2 is required")
class KerasGraphContracts(unittest.TestCase):
    def test_keras_graph_conversion(self):
        if not importlib.util.find_spec("tensorflow"):self.skipTest("Optional TensorFlow/Keras 2 is required")
        import tensorflow as tf
        if int(tf.__version__.split(".")[1]) >= 16 and int(getattr(tf.keras,"__version__","2").split(".")[0]) >= 3:
            self.skipTest("Pinned Graph.from_keras supports Keras 2, not Keras 3")
        left=tf.keras.Input((2,),name="left");right=tf.keras.Input((2,),name="right")
        merged=tf.keras.layers.Concatenate(name="merge")([left,right])
        output=tf.keras.layers.Dense(1,name="out")(merged)
        model=tf.keras.Model([left,right],output)
        graph=Graph.from_keras(model)
        self.assertEqual([n.id for n in graph.nodes],["left","right","merge","out"])
        self.assertEqual([edge.to_json() for edge in graph.edges],[["left","merge"],["right","merge"],["merge","out"]])
        self.assertEqual(graph["out"].num_parameters,5)
