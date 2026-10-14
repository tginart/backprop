"""Exercise shared run handles in separate Python processes, not only objects.

Architecture: concurrent writer subprocesses -> combined/reordered events ->
public replay. Each writer's independent journal and steps must survive merging.
Main callers: base contract CI.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from backprop.apis.public.replay import reduce_events


class Processes(unittest.TestCase):
    def test_independent_writers_converge_without_erasing_other_metrics(self):
        with tempfile.TemporaryDirectory() as root:
            processes=[]
            for label,primary in [('rank0','true'),('rank1','false')]:
                output=str(Path(root)/(label+'.json'))
                process=subprocess.Popen([sys.executable,'-B','tests/shared_writer_fixture.py',root,label,primary,output],env={**os.environ,'PYTHONPATH':'sdk'})
                processes.append((process,output))
            events=[]
            for process,output in processes:
                self.assertEqual(process.wait(timeout=30),0)
                events.extend(json.loads(Path(output).read_text()))
            state=reduce_events(events)
            replay=reduce_events(list(reversed(events))+events)
            self.assertEqual(state,replay)
            self.assertEqual(state['state'],'finished')
            self.assertTrue(state['config']['rank0']);self.assertTrue(state['config']['rank1'])
            self.assertEqual(state['summary']['rank0/loss'],1)
            self.assertEqual(state['summary']['rank1/loss'],1)
            for writer in state['writers']:
                self.assertEqual([r['_step'] for r in state['history'] if r['_writer']==writer],[0,1,2])
