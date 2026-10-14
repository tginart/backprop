/** Actual SDK output events must produce the same browser relationships. */
import test from 'node:test';
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';
import {normalizeEvents} from '../web/model.mjs';

test('publication survives hard exit, delayed source sync and shared output reuse',()=>{
  const fixture=JSON.parse(execFileSync(process.env.BACKPROP_PYTHON || 'python3',
    ['-B','tests/artifact_logging_replay_fixture.py'],{encoding:'utf8',env:{...process.env,PYTHONPATH:'sdk'}}));
  for(const events of [fixture.delayed_events,fixture.events]) {
    const view=normalizeEvents([...events].reverse().concat(events));
    assert.deepEqual(view.issues,[]);
    for(const run of view.runs) {
      if(!fixture.outputs[run.id]) continue;
      assert.equal(run.artifacts.length,1);
      assert.equal(run.artifacts[0].artifact_id,fixture.artifact_id);
    }
  }
  const view=normalizeEvents(fixture.events);
  for(const run of view.runs) {
    if(!fixture.outputs[run.id]) continue;
    assert.deepEqual(run.artifacts.map(({artifact_id,version,tags,aliases})=>({artifact_id,version,tags,aliases})),fixture.outputs[run.id]);
  }
  assert(fixture.recovered_after_hard_exit && fixture.file_bytes_verified);
});
