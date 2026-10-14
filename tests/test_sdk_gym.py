"""Verify Gym capture, isolation, aliases, failures and journal recovery.

Architecture: hashed pinned adapter subprocesses -> real Backprop video logging
with controlled recorder lifecycles -> durable restart and public API readback.
Main caller: contract CI. Real optional frameworks run in test_optional_gym.py.
"""
import ast
import hashlib
import importlib
import inspect
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import backprop as w
from backprop.apis.public import Api
from backprop.integration import gym
from backprop.transport import read_journal, sync
from gym_fixtures import library

ROOT=Path(__file__).resolve().parents[1]


class GymContracts(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.addCleanup(w.teardown)
        self.store=w.MemorySink()
        self.modules=library()
        self.environment=patch.dict(sys.modules,self.modules);self.environment.start()
        self.addCleanup(self.environment.stop)
        self.addCleanup(gym._unpatch)

    def run_handle(self, **kwargs):
        run=w.init(project="gym",entity="e",dir=self.tmp.name,_sink=self.store,
                   settings={"console":"off","silent":True,"x_disable_stats":True},**kwargs)
        self.addCleanup(run.finish)
        return run

    def rows(self, run):
        return [e["payload"]["metrics"] for e in read_journal(run.sync_dir) if e["type"]=="history"]

    def modern(self, name="episode-0"):
        recorder=self.modules["gymnasium.wrappers"].RecordVideo(self.tmp.name)
        recorder.start_recording(name);recorder.recorded_frames=[1]
        return recorder

    def test_aliases_signature_idempotence_and_process_teardown(self):
        from backprop.compatibility import install_wandb_aliases
        install_wandb_aliases()
        aliases=[w.gym,importlib.import_module("wandb.gym"),importlib.import_module("wandb.integration.gym")]
        self.assertTrue(all(alias is gym for alias in aliases))
        recorder=self.modules["gymnasium.wrappers"].RecordVideo
        original=recorder.stop_recording
        for alias in aliases:
            self.assertEqual(str(inspect.signature(alias.monitor)),"()")
            self.assertIsNone(alias.monitor())
        hooked=recorder.stop_recording
        self.assertIsNot(hooked,original)
        run=self.run_handle(monitor_gym=True)
        self.assertIs(recorder.stop_recording,hooked)
        self.modern().close();self.assertEqual(len(self.rows(run)),1)
        w.teardown();self.assertIs(recorder.stop_recording,original)
        second=self.run_handle(monitor_gym=True)
        self.modern("episode-1").close();self.assertEqual(len(self.rows(second)),1)

    def test_modern_segments_and_original_return_errors(self):
        run=self.run_handle(monitor_gym=True)
        recorder=self.modern()
        recorder.start_recording("episode-1")
        self.assertEqual(len(self.rows(run)),1)
        recorder.recorded_frames=[2]
        self.assertIsNone(recorder.close());self.assertIsNone(recorder.close())
        self.assertEqual(len(self.rows(run)),2)
        with self.assertRaises(AssertionError):recorder.stop_recording()
        empty=self.modules["gymnasium.wrappers"].RecordVideo(self.tmp.name)
        empty.start_recording("empty");empty.close()
        self.assertEqual(len(self.rows(run)),2)

    def test_legacy_disabled_empty_broken_and_destructor_do_not_publish(self):
        gym._unpatch();modules=library("gymnasium","0.29.1")
        with patch.dict(sys.modules,modules):
            run=self.run_handle(monitor_gym=True)
            cls=modules["gymnasium.wrappers.monitoring.video_recorder"].VideoRecorder
            for options in ({"enabled":False},{"empty":True},{"broken":True}):
                recorder=cls(Path(self.tmp.name)/"none.mp4",**options);recorder.close()
            recorder=cls(Path(self.tmp.name)/"empty-modern-legacy.mp4")
            del recorder.empty;recorder.recorded_frames=[];recorder.close()
            recorder=cls(Path(self.tmp.name)/"destructor.mp4");recorder.__del__()
            self.assertEqual(self.rows(run),[])
            recorder=cls(Path(self.tmp.name)/"openai.video.4.mp4");recorder.close();recorder.close()
            rows=self.rows(run);self.assertEqual(len(rows),1);self.assertIn("video.4",rows[0])
            run.flush()
            value=Api._from_store(self.store,project="gym",entity="e").run(run.id).summary["video.4"]
            self.assertEqual(self.store.download_file(value),b"legacy-mp4")

    def test_encoder_and_local_journal_errors_are_visible_and_retryable(self):
        run=self.run_handle(monitor_gym=True);recorder=self.modern()
        recorder.fail=True
        with self.assertRaisesRegex(OSError,"encoder failed"):recorder.close()
        self.assertEqual(self.rows(run),[])
        recorder.fail=False
        original=run.log
        with patch.object(run,"log",side_effect=OSError("local write failed")):
            with self.assertRaisesRegex(OSError,"local write failed"):recorder.close()
        self.assertFalse(recorder.recording)
        with patch.object(run,"log",wraps=original) as logged:
            recorder.close();recorder.close();self.assertEqual(logged.call_count,1)
        self.assertEqual(len(self.rows(run)),1)

    def test_finished_owner_does_not_leak_into_a_new_run(self):
        first=self.run_handle(monitor_gym=True);recorder=self.modern()
        first.finish();second=self.run_handle()
        with self.assertWarnsRegex(RuntimeWarning,"after its Backprop run finished"):recorder.close()
        self.assertEqual(self.rows(first),[]);self.assertEqual(self.rows(second),[])
        recorder.start_recording("next-session");recorder.recorded_frames=[2];recorder.close()
        self.assertEqual(len(self.rows(second)),1)

    def test_monitor_before_init_and_create_new_respects_global_binding(self):
        gym.monitor();recorder=self.modern()
        first=self.run_handle();recorder.close();self.assertEqual(len(self.rows(first)),1)
        second=self.run_handle(reinit="create_new")
        self.modern("global-owner").close()
        self.assertEqual(len(self.rows(first)),2);self.assertEqual(self.rows(second),[])

    def test_offline_snapshot_survives_source_deletion_and_duplicate_delivery(self):
        run=w.init(project="gym",entity="e",dir=self.tmp.name,mode="offline",monitor_gym=True,
                   settings={"console":"off","silent":True,"x_disable_stats":True})
        self.addCleanup(run.finish)
        recorder=self.modern();recorder.close();run.finish()
        (Path(self.tmp.name)/"episode-0.mp4").unlink()
        sync(run.sync_dir,sink=self.store)
        self.store.events += list(reversed(self.store.events))
        api=Api._from_store(self.store,project="gym",entity="e")
        public=api.run(run.id);rows=list(public.scan_history())
        self.assertEqual(len(rows),1)
        self.assertEqual(self.store.download_file(rows[0]["videos"]),b"modern-mp4")
        before=len(self.store.events);sync(run.sync_dir,sink=self.store)
        self.assertEqual(len(self.store.events),before)

    def test_import_failure_is_not_hidden_by_falling_back_to_another_library(self):
        original=importlib.import_module
        def absent(name,*args,**kwargs):
            if name in {"gymnasium","gym"}:raise ModuleNotFoundError(name,name=name)
            return original(name,*args,**kwargs)
        with patch.object(gym.importlib,"import_module",side_effect=absent):
            with self.assertRaisesRegex(w.Error,"Couldn't import"):gym.monitor()
        def broken(name,*args,**kwargs):
            if name=="gymnasium":raise ModuleNotFoundError("numpy",name="numpy")
            return original(name,*args,**kwargs)
        with patch.object(gym.importlib,"import_module",side_effect=broken):
            with self.assertRaises(ModuleNotFoundError) as caught:gym.monitor()
        self.assertEqual(caught.exception.name,"numpy")

    def test_unpatch_keeps_later_third_party_hooks(self):
        run=self.run_handle(monitor_gym=True)
        cls=self.modules["gymnasium.wrappers"].RecordVideo;backprop_close=cls.close
        def third_party(self):return backprop_close(self)
        cls.close=third_party
        gym._unpatch();self.assertIs(cls.close,third_party)
        self.modern().close();self.assertEqual(self.rows(run),[])

    def probe(self,backend,name,version):
        result=subprocess.run([sys.executable,"-B",str(ROOT/"tests/gym_reference_probe.py"),backend,name,version],
            env={**os.environ,"PYTHONPATH":str(ROOT/"sdk")},text=True,capture_output=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stderr)
        return json.loads(result.stdout)

    @unittest.skipUnless((ROOT/".reference/wandb/source").exists(),"Pinned W&B source required")
    def test_pinned_legacy_key_selection_disabled_recorders_and_return(self):
        for name,version in (("gym","0.26.2"),("gymnasium","0.29.1")):
            with self.subTest(name=name):self.assertEqual(self.probe("reference",name,version),self.probe("native",name,version))

    @unittest.skipUnless((ROOT/".reference/wandb/source").exists(),"Pinned W&B source required")
    def test_old_encoder_and_modern_recorder_reference_bugs_are_explicit(self):
        for name,version in (("gym","0.25.2"),("gymnasium","1.0.0")):
            with self.subTest(name=name):
                self.assertEqual(self.probe("reference",name,version)["error"],"AttributeError")
                native=self.probe("native",name,version)
                self.assertIsNone(native["error"]);self.assertTrue(native["logs"])


class FabricGraphNoop(unittest.TestCase):
    @unittest.skipUnless((ROOT/".reference/wandb/source").exists(),"Pinned W&B source required")
    def test_pinned_full_signature_return_and_no_model_execution(self):
        paths=[ROOT/".reference/wandb/source/wandb/integration/lightning/fabric/logger.py",
               ROOT/"sdk/backprop/integration/lightning/fabric/logger.py"]
        self.assertEqual(hashlib.sha256(paths[0].read_bytes()).hexdigest(),"7d8f86bf6f31356492ab80239fffcc90dbed59d56cb0e77d9ffb8ce60c2ddc57")
        methods=[]
        for path in paths:
            tree=ast.parse(path.read_text())
            cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=="WandbLogger")
            method=next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=="log_graph")
            namespace={}
            module=ast.fix_missing_locations(ast.Module(body=[ast.ImportFrom(module="__future__",names=[ast.alias(name="annotations")],level=0),method],type_ignores=[]))
            exec(compile(module,str(path),"exec"),namespace);methods.append(namespace["log_graph"])
        self.assertEqual(inspect.signature(methods[0]),inspect.signature(methods[1]))
        class NoAccess:
            def __getattribute__(self,name):raise AssertionError("No model/array execution permitted")
        for method in methods:
            self.assertIsNone(method(NoAccess(),NoAccess()))
            self.assertIsNone(method(NoAccess(),NoAccess(),input_array=NoAccess()))
            with self.assertRaises(TypeError):method(None)
            with self.assertRaises(TypeError):method(None,None,unexpected=True)
