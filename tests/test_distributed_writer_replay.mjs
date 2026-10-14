/** Replay actual shared-writer restart journals through the browser reducer. */
import test from 'node:test';
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';
import {normalizeEvents} from '../web/model.mjs';
test('unlabeled writers stay separate and labeled writers recover after exit',()=>{
  const fixtures=JSON.parse(execFileSync(process.env.BACKPROP_PYTHON||'python3',
    ['-B','tests/distributed_writer_replay_fixture.py'],{encoding:'utf8',env:{...process.env,PYTHONPATH:'sdk'}}));
  for(const fixture of fixtures){
    const view=normalizeEvents([...fixture.events].reverse().concat(fixture.events));
    assert.deepEqual(view.issues,[]);
    const run=view.runs.find(r=>r.id==='shared' && r.project==='p'),expected=fixture.expected;
    assert.equal(run.state,expected.state);assert.deepEqual(JSON.parse(JSON.stringify(run.config)),expected.config);
    assert.deepEqual(run.artifacts.map(({artifact_id,version,contribution_ids})=>({artifact_id,version,contribution_ids})),
      [{artifact_id:expected.artifact_id,version:expected.version,contribution_ids:expected.contribution_ids}]);
    assert.deepEqual(run.pending_artifacts,[]);
    assert(fixture.hard_exit_recovered && fixture.original_journal_unchanged);
  }
});
