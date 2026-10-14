/** Cross-project output and input recovery must agree with SDK queries. */
import test from 'node:test';
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';
import {normalizeEvents} from '../web/model.mjs';

test('cross-project artifact copy retains destination identity after consumer crash',()=>{
  const fixture=JSON.parse(execFileSync(process.env.BACKPROP_PYTHON || 'python3',
    ['-B','tests/artifact_namespace_replay_fixture.py'],{encoding:'utf8',env:{...process.env,PYTHONPATH:'sdk'}}));
  for(const events of [fixture.delayed_events,fixture.events]){
    const view=normalizeEvents([...events].reverse().concat(events));
    assert.deepEqual(view.issues,[]);
  }
  const view=normalizeEvents([...fixture.events].reverse().concat(fixture.events));
  for(const [id,expected] of Object.entries(fixture.runs)){
    const run=view.runs.find(run=>run.id===id && run.project===expected.project);
    assert.equal(run.state,expected.state);
    assert.deepEqual(JSON.parse(JSON.stringify(run.config)),expected.config);
    assert.deepEqual(run.artifacts.map(({artifact_id,version,aliases,tags})=>({artifact_id,version,aliases,tags})),expected.outputs);
    assert.deepEqual(run.used_artifacts,expected.used_artifacts);
  }
  assert(fixture.recovered_after_hard_exit && fixture.file_bytes_verified && fixture.source_removed);
});
