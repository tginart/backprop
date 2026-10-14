/** Shared group closure decisions must reconstruct identically in both clients. */
import test from 'node:test';
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';
import {normalizeEvents} from '../web/model.mjs';
test('racing distributed finalizers recover canonical IDs after a process exit',()=>{
  const fixture=JSON.parse(execFileSync(process.env.BACKPROP_PYTHON||'python3',
    ['-B','tests/distributed_artifact_closure_replay_fixture.py'],{encoding:'utf8',env:{...process.env,PYTHONPATH:'sdk'}}));
  const view=normalizeEvents([...fixture.events].reverse().concat(fixture.events));
  assert.deepEqual(view.issues,[]);
  for(const [id,expected] of Object.entries(fixture.runs)){
    const run=view.runs.find(r=>r.id===id && r.project==='p');assert.equal(run.state,expected.state);
    assert.deepEqual(JSON.parse(JSON.stringify(run.config)),expected.config);
    assert.deepEqual(run.artifacts.map(({artifact_id,version,contribution_ids})=>({artifact_id,version,contribution_ids})),expected.outputs);
    assert.deepEqual(run.used_artifacts.map(({artifact_id,name,version,qualified_name})=>({artifact_id,name,version,qualified_name})),expected.inputs);
    assert.deepEqual(run.pending_artifacts,[]);
  }
  assert(fixture.claim_exit_recovered && fixture.disjoint_generations && fixture.original_journals_unchanged && fixture.exact_file_bytes);
});
