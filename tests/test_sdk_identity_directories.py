"""Verify inherited media directory contracts and Foundry project equivalents.

Architecture: hash-checked upstream directory methods -> independent aliases;
real run/artifact events -> project IDs stable across duplicate/reordered replay.
Main caller: SDK CI. W&B database IDs are deliberately not invented or queried.
"""
import ast
import base64
import hashlib
import importlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

import backprop as w
from backprop.apis.public import Api
from backprop.media import BoundingBoxes2D, Classes

ROOT=Path(__file__).resolve().parents[1]


class IdentityDirectories(unittest.TestCase):
    @unittest.skipUnless((ROOT/".reference/wandb/source").exists(),"Pinned W&B source required")
    def test_inherited_directory_methods_and_aliases_match_pinned_source(self):
        from backprop.compatibility import install_wandb_aliases
        install_wandb_aliases()
        for file,cls,native,expected_hash in (
                ("media.py","Media",Classes,"9c2ea72e84eb2b07c91ddaa4e5ca94e70aff0c25ede272bf5ccc257efe61f856"),
                ("json_metadata.py","JSONMetadata",BoundingBoxes2D,"38d389db1ccf825b21cb95b145bc6b0be6ad5745687c374a3b0db5059b73cc98")):
            path=ROOT/".reference/wandb/source/wandb/sdk/data_types/base_types"/file
            source=path.read_bytes();self.assertEqual(hashlib.sha256(source).hexdigest(),expected_hash)
            tree=ast.parse(source)
            node=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name==cls)
            method=next(n for n in node.body if isinstance(n,ast.FunctionDef) and n.name=="get_media_subdir")
            method.decorator_list=[]
            tree=ast.fix_missing_locations(ast.Module(body=[ast.ImportFrom(module="__future__",names=[ast.alias(name="annotations")],level=0),method],type_ignores=[]))
            import os
            namespace={"os":os};exec(compile(tree,str(path),"exec"),namespace)
            reference=namespace["get_media_subdir"]
            aliases=[getattr(w.data_types,native.__name__)]
            if native is Classes:aliases.append(w.Classes)
            for alias in aliases:
                self.assertIs(alias,native)
                if native is Classes:
                    with self.assertRaises(NotImplementedError):reference(alias)
                    with self.assertRaises(NotImplementedError):alias.get_media_subdir()
                else:self.assertEqual(alias.get_media_subdir(),reference(alias))
                with self.assertRaises(TypeError):alias.get_media_subdir("unexpected")
            class Derived(native):
                @classmethod
                def type_name(cls):return "extension-boxes"
            if native is BoundingBoxes2D:self.assertEqual(Derived.get_media_subdir(),reference(Derived))

    def test_project_ids_agree_across_queries_edits_aliases_and_replay(self):
        store=w.MemorySink()
        with tempfile.TemporaryDirectory() as directory:
            run=w.init(entity="e",project="project",dir=directory,_sink=store,
                       settings={"console":"off","silent":True,"x_disable_stats":True})
            artifact=w.Artifact("ids","dataset")
            with artifact.new_file("data.txt") as stream:stream.write("identity")
            run.log_artifact(artifact).wait();run.finish()
            api=Api._from_store(store,entity="e",project="project")
            project=api.project("project","e")
            collection=api.artifact_collection("dataset","e/project/ids")
            public=api.run(run.id)
            integer=public.project_internal_id
            self.assertIs(type(integer),int);self.assertGreater(integer,0)
            self.assertEqual(project.id,collection.project_id)
            self.assertEqual(integer,int(project.id,16))
            self.assertEqual(base64.b64decode(collection.project_internal_id).decode(),f"BackpropProject:{integer}")
            public.name="edited";public.update()
            collection.description="edited";collection.save()
            replay=w.MemorySink();replay.events=list(reversed(store.events))+store.events
            restarted=Api._from_store(replay,entity="e",project="project")
            self.assertEqual(restarted.run(run.id).project_internal_id,integer)
            self.assertEqual(restarted.artifact_collection("dataset","e/project/ids").project_internal_id,collection.project_internal_id)
            self.assertEqual(run.project,"project")
            from backprop.apis.public.projects import project_internal_identity
            self.assertNotEqual(project_internal_identity("another","project"),integer)
            self.assertNotEqual(project_internal_identity("e","another"),integer)
            self.assertEqual(project_internal_identity("e","project"),integer)
