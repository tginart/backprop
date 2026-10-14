"""Observe output relationships for independent distributed contributor runs.

Architecture: real worker upsert -> primary finish -> refresh worker handle ->
public Run.logged_artifacts and source journal. This is a native observation,
not a fabricated hosted-W&B response. Source-backed expected lineage is recorded
separately from the observed missing relationship.
"""
import json,os,tempfile
from pathlib import Path
from unittest.mock import patch
import backprop as w

def observe():
 with tempfile.TemporaryDirectory() as directory,patch.dict(os.environ,{'BACKPROP_DIR':directory}):
  sink=w.MemorySink()
  settings={'console':'off','silent':True,'disable_git':True,'x_disable_stats':True,'flush_interval':600}
  def run(name):return w.init(id=name,entity='e',project='p',group='parts',dir=directory,_sink=sink,_deployment={},settings=settings,reinit='create_new')
  def part(name):
   artifact=w.Artifact('model','model')
   with artifact.new_file(name) as out:out.write(name)
   return artifact
  with run('worker') as worker:handle=worker.upsert_artifact(part('worker'))
  original=Path(worker.sync_dir,'events.jsonl').read_bytes()
  with run('primary') as primary:primary.finish_artifact(part('primary'))
  handle.wait();api=w.Api._from_store(sink,entity='e',project='p')
  return {'sdk_version':w.__version__,'handle_committed':handle.state=='COMMITTED',
   'combined_files':sorted(handle.manifest.entries),
   'worker_output_count':len(list(api.run('worker').logged_artifacts())),
   'primary_output_count':len(list(api.run('primary').logged_artifacts())),
   'source_journal_unchanged':Path(worker.sync_dir,'events.jsonl').read_bytes()==original}

if __name__=='__main__':print(json.dumps(observe()))
