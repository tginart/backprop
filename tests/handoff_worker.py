"""Receive a trusted test descriptor in a separate process and log one writer.

Architecture: locally generated pickle -> secondary handle -> durable journal
and JSON observations. Main callers: test_sdk_run_handoff; no credentials or
network services are used by these fixtures.
"""
import json
from pathlib import Path
import pickle
import sys
from backprop.run_handoff import restore_run
from backprop.transport import read_journal

if __name__ == '__main__':
    descriptor, label, output = sys.argv[1:]
    run = restore_run(pickle.loads(Path(descriptor).read_bytes()))
    run.config.update({label:True})
    run.log({'epoch':1,label:1})
    run.log({'epoch':2,label:2})
    scope = [run.entity,run.project,run.id]
    run.finish()
    Path(output).write_text(json.dumps({'events':read_journal(run.sync_dir), 'scope':scope,
        'mode':run.mode, 'step':run.step,'dir':run.sync_dir,'lr':run.config['lr'],
        'writer':run._writer_id,'primary':run.settings.x_primary}))
