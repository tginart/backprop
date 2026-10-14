"""Emit actual checkpoint retry/restart records for Python/browser agreement.

Architecture: native checkpoint writes and lost acknowledgement -> durable
journal resume -> public artifact view -> browser fixture. Main caller:
test_checkpoint_replay.mjs; shuffled duplicate deliveries must retain one ID.
"""
import json
from pathlib import Path
import tempfile
from unittest.mock import patch
import backprop
from backprop.integration.checkpoint_artifacts import log_checkpoint
from backprop.apis.public import Api
from backprop.transport import MemorySink

with tempfile.TemporaryDirectory() as directory:
    sink=MemorySink(); path=Path(directory)/'weights'; path.write_bytes(b'checkpoint')
    options=dict(entity='e',project='p',id='r',dir=directory,_sink=sink,_deployment={},
                 settings={'silent':True,'console':'off','x_disable_stats':True,'disable_git':True})
    def artifact():
        value=backprop.Artifact('weights','model',metadata={'score':1})
        value.add_file(path,name='model.ckpt'); return value
    run=backprop.Run(**options); original=run.log_model
    def failed(*args,**kwargs):
        original(*args,**kwargs); raise OSError('lost acknowledgement')
    with patch.object(run,'log_model',side_effect=failed):
        try: log_checkpoint(run,artifact(),['latest'])
        except OSError: pass
    log_checkpoint(run,artifact(),['latest','best']); run.finish()
    run=backprop.Run(**options,resume='allow')
    log_checkpoint(run,artifact(),['restored']); run.finish()
    api=Api._from_store(sink,entity='e',project='p')
    print(json.dumps({'events':sink.events,'artifacts':[{'id':a.id,'version':a.version,
        'aliases':a.aliases,'metadata':dict(a.metadata),'bytes':a.get_entry('model.ckpt')._read_verified_bytes().decode()}
        for a in api.run('r').logged_artifacts()]}))
