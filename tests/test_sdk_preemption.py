"""Immediate preemption notifications through the existing bounded uploader."""
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from importlib import import_module
from unittest.mock import patch

import backprop
from backprop.transport import sync


class PreemptionContracts(unittest.TestCase):
    def test_online_marker_wakes_long_interval_without_blocking_caller(self):
        with tempfile.TemporaryDirectory() as directory:
            sink=backprop.MemorySink();entered=threading.Event();release=threading.Event()
            run=backprop.Run(project='preemption',dir=directory,_sink=sink,_deployment={},
                settings={'console':'off','silent':True,'disable_git':True,'flush_interval':3600,'heartbeat_seconds':3600,'finish_timeout':.02})
            publish=sink.publish
            def blocking(events):
                entered.set();release.wait(2);publish(events)
            sink.publish=blocking
            try:
                before=time.monotonic();self.assertIsNone(run.mark_preempting())
                self.assertLess(time.monotonic()-before,.3)
                self.assertTrue(entered.wait(1),'Preemption must bypass the one-hour interval')
                before=time.monotonic();run.finish()
                self.assertLess(time.monotonic()-before,.5)
                self.assertTrue(any('finish exceeded' in warning for warning in run._warnings))
                release.set();run._thread.join(timeout=1)
                sink.publish=publish;sync(run.sync_dir,sink=sink)
                markers=[e for e in sink.events if e['type']=='run_metadata' and e['payload']['values'].get('preempting')]
                self.assertEqual(len(markers),1)
                self.assertEqual(sum(e['type']=='run_finish' for e in sink.events),1)
            finally:
                release.set();sink.publish=publish
                if not run._closed:run.finish()

    def test_offline_marker_is_durable_without_uploader_and_disabled_is_noop(self):
        with tempfile.TemporaryDirectory() as directory:
            run=backprop.Run(project='preemption',dir=directory,mode='offline',settings={'console':'off','silent':True,'disable_git':True})
            self.assertIsNone(run.mark_preempting());self.assertIsNone(run._thread);run.finish()
            records=[json.loads(line) for line in Path(run.sync_dir,'events.jsonl').read_text().splitlines()]
            self.assertTrue(any(e['payload'].get('values',{}).get('preempting') for e in records))
            with patch.object(import_module("backprop.run"), "Journal",side_effect=AssertionError('disabled must not write')):
                disabled=backprop.Run(mode='disabled',dir=Path(directory)/'disabled')
                self.assertIsNone(disabled.mark_preempting());disabled.finish()
            self.assertFalse(Path(directory,'disabled').exists())

    def test_heartbeat_setting_controls_due_metadata_without_changing_run_state(self):
        with tempfile.TemporaryDirectory() as directory:
            sink=backprop.MemorySink();seen=threading.Event()
            run=backprop.Run(project='heartbeat',dir=directory,_sink=sink,_deployment={},
                settings={'console':'off','silent':True,'disable_git':True,'flush_interval':3600,'heartbeat_seconds':1})
            original=sink.publish
            def publish(events):
                original(events)
                if any('heartbeat_at' in e['payload'].get('values',{}) for e in events):seen.set()
            sink.publish=publish
            try:
                run._last_heartbeat=time.monotonic()-2
                run._wake.set();self.assertTrue(seen.wait(1))
                heartbeat=next(e['payload']['values'] for e in sink.events if 'heartbeat_at' in e['payload'].get('values',{}))
                self.assertGreaterEqual(heartbeat['runtime'],0)
                self.assertTrue(heartbeat['heartbeat_at'].endswith('+00:00'))
                self.assertFalse(run._closed)
            finally:run.finish()

if __name__=='__main__':unittest.main()
