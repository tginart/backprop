"""Complete base media construction, explicit binding and extension round trips.

Architecture: isolated pinned-wheel comparison plus real Backprop run journals
and artifact reads. The tests exercise nondefault options instead of only class
presence. Main caller: SDK CI; optional W&B absence is an explicit skip.
"""
import importlib.util
import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import backprop as w
from backprop.media import Media,BatchableMedia,EncodedMedia
from backprop.values import WBValue

ROOT=Path(__file__).resolve().parents[1]


class MediaBaseContracts(unittest.TestCase):
    def test_pinned_file_lifecycle_and_base_constructors(self):
        try:
            reference = importlib.metadata.distribution("wandb")
        except importlib.metadata.PackageNotFoundError:
            self.skipTest("Pinned W&B wheel unavailable; the Backprop alias is not a reference")
        self.assertEqual(reference.version, "0.30.0")
        outputs=[]
        for mode in ("reference","native"):
            env={**os.environ,"PYTHONPATH":str(reference.locate_file("")) if mode=="reference" else str(ROOT/"sdk")}
            result=subprocess.run([sys.executable,"-B",str(ROOT/"tests/media_base_probe.py"),mode],cwd=tempfile.gettempdir(),env=env,text=True,capture_output=True,timeout=30)
            if mode=="reference" and "No module named 'wandb'" in result.stderr:self.skipTest("Pinned W&B wheel unavailable")
            self.assertEqual(result.returncode,0,result.stderr)
            outputs.append(json.loads(result.stdout))
        self.assertEqual(outputs[0],outputs[1])

    def test_public_aliases_keep_caption_constructor_and_batch_inheritance(self):
        from backprop.compatibility import install_wandb_aliases
        install_wandb_aliases()
        from wandb.data_types import Media as PublicMedia,BatchableMedia as PublicBatch
        from wandb.sdk.data_types.base_types import Media as BaseMedia,BatchableMedia as BaseBatch
        from wandb.sdk.data_types.base_types.media import Media as ModuleMedia,BatchableMedia as ModuleBatch
        self.assertTrue(all(value is Media for value in (PublicMedia,BaseMedia,ModuleMedia)))
        self.assertTrue(all(value is BatchableMedia for value in (PublicBatch,BaseBatch,ModuleBatch)))
        for cls in (w.Image,w.Audio,w.Video,w.Html):self.assertTrue(issubclass(cls,BatchableMedia))

    def test_named_bindings_publish_durable_files_and_honor_rebind_options(self):
        with tempfile.TemporaryDirectory() as directory:
            store=w.MemorySink()
            with w.init(project="binding",dir=directory,_sink=store,settings={"console":"off","silent":True,"x_disable_stats":True}) as run:
                media=w.Html("<p>Bound</p>",inject=False)
                self.assertIsNone(media.bind_to_run(run,"html/sub",12,id_="chosen"))
                bound=Path(run.dir)/"media/html/html/sub_12_chosen.html"
                self.assertTrue(bound.is_file());self.assertEqual(bound.read_bytes(),b"<p>Bound</p>")
                self.assertTrue(media.is_bound())
                with self.assertRaises(Exception) as caught:media.bind_to_run(run,"html/sub",12,id_="chosen")
                self.assertEqual(type(caught.exception).__name__,"SameFileError")
                media.bind_to_run(run,"html/sub",12,id_="chosen",ignore_copy_err=True)
                run.log({"value":media})
            event=next(e for e in store.events if e["type"]=="history")
            self.assertEqual(store.download_file(event["payload"]["metrics"]["value"]),b"<p>Bound</p>")
            self.assertTrue(any(e["type"]=="file" for e in store.events))

    def test_media_subclasses_keep_custom_serializers_and_decoders(self):
        previous=WBValue._type_mapping;self.addCleanup(setattr,WBValue,"_type_mapping",previous)
        class CustomFile(w.Html):
            _log_type="extension-file-probe"
            def __init__(self,path):Media.__init__(self,caption="extension");self._set_file(str(path))
            @classmethod
            def get_media_subdir(cls):return "media/custom"
            def to_json(self,owner):return {**Media.to_json(self,owner),"marker":"preserved"}
            @classmethod
            def from_json(cls,data,owner):return cls(owner.get_entry(data["path"]).download(root=directory))
        class CustomTable(w.Table):
            _log_type="extension-table-probe"
            def to_json(self,owner):return {"_type":self._log_type,"rows":self.data,"marker":"preserved"}
            @classmethod
            def from_json(cls,data,owner):return cls(columns=["x"],data=data["rows"])
        WBValue._type_mapping=None
        with tempfile.TemporaryDirectory() as directory:
            source=Path(directory)/"content.bin";source.write_bytes(b"custom")
            artifact=w.Artifact("custom-media","dataset")
            file_entry=artifact.add(CustomFile(source),"file")
            table_entry=artifact.add(CustomTable(columns=["x"],data=[[4]]),"table")
            self.assertEqual(json.loads(file_entry._read_verified_bytes())["marker"],"preserved")
            self.assertTrue(table_entry.path.endswith(".extension-table-probe.json"))
            self.assertEqual(artifact.get("file")._caption,"extension")
            self.assertIsInstance(artifact.get("file"),CustomFile)
            self.assertIsInstance(artifact.get("table"),CustomTable)

    def test_extension_override_can_call_super_and_be_inherited(self):
        old=WBValue._type_mapping;self.addCleanup(setattr,WBValue,"_type_mapping",old)
        class Parent(w.Table):
            _log_type="super-table-probe"
            def to_json(self,owner):return {**super().to_json(owner),"marker":"from-parent"}
        class Child(Parent):pass
        WBValue._type_mapping=None
        artifact=w.Artifact("super-value","dataset")
        for cls in (Parent,Child):
            entry=artifact.add(cls(columns=["x"],data=[[1]]),cls.__name__)
            self.assertEqual(json.loads(entry._read_verified_bytes())["marker"],"from-parent")

    def test_arbitrary_base_values_remain_invalid_top_level_artifact_inputs(self):
        class Extension(WBValue):
            _log_type="non-artifact-extension"
            def to_json(self,owner):return {"_type":self._log_type,"value":3}
        with self.assertRaises(TypeError):w.Artifact("invalid","dataset").add(Extension(),"x")

    def test_link_failures_fall_back_to_atomic_copy(self):
        from backprop.media_base import _link_or_copy
        with tempfile.TemporaryDirectory() as directory:
            source=Path(directory)/"source";source.write_bytes(b"contents")
            destination=Path(directory)/"target"
            with patch('pathlib.Path.symlink_to',side_effect=OSError("unavailable")),patch('backprop.media_base.os.link',side_effect=OSError("different device")):
                _link_or_copy(source,destination,symlink=True)
            self.assertEqual(destination.read_bytes(),b"contents")
            source.write_bytes(b"changed")
            self.assertEqual(destination.read_bytes(),b"contents")
            self.assertFalse(destination.with_name("target.tmp~backprop").exists())

    def test_native_file_values_keep_content_identity_after_base_refactor(self):
        for cls,options in ((w.Html,{"inject":False}),(w.Video,{"format":"webm"})):
            a,b,c=cls(b"same",**options),cls(b"same",**options),cls(b"different",**options)
            self.assertEqual(a,b);self.assertNotEqual(a,c)
            self.assertEqual(a._size,4);self.assertTrue(a.file_is_set())
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/"same.html";path.write_bytes(b"same")
            self.assertEqual(w.Html(path,inject=False),w.Html(b"same",inject=False))
