"""Actual artifact identities in Python reads and browser event reconstruction.

Architecture: ordinary run journal -> acknowledged publication -> public JSON
reference/commit identity -> same events consumed by the production browser model.
Main callers: JavaScript replay and release evidence. No services are required.
"""
import json,os,sys,tempfile
from unittest.mock import patch
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'sdk'))
import backprop as w
from test_artifact_identity import SETTINGS

with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'BACKPROP_DIR':directory}):
 sink=w.MemorySink()
 with w.init(id='identity-replay',entity='e',project='identity',dir=directory,_sink=sink,_deployment={},settings=SETTINGS) as run:
  artifact=w.Artifact('dataset','data');path=Path(directory)/'data.txt';path.write_text('immutable')
  artifact.add_file(path);run.log_artifact(artifact);run.flush()
  reference=artifact.json_encode();commit=artifact.commit_hash
  run.config['dataset']=reference
  artifact.description='updated description';artifact.save()
 print(json.dumps({'events':sink.events,'reference':reference,'commit':commit,
  'config':w.Api._from_store(sink,entity='e',project='identity').run(run.id).config},sort_keys=True))
