/** Model input relationships must agree after duplicate/reordered replay. */
import test from 'node:test';
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';
import {normalizeEvents} from '../web/model.mjs';
test('model input relationships survive delayed publication and consumer restart',()=>{
  const fixture=JSON.parse(execFileSync(process.env.BACKPROP_PYTHON || 'python3',
    ['-B','tests/model_use_replay_fixture.py'],{encoding:'utf8',env:{...process.env,PYTHONPATH:'sdk'}}));
  const view=normalizeEvents([...fixture.events].reverse().concat(fixture.events));
  assert.deepEqual(view.issues,[]);
  const run=view.runs.find(run=>run.id==='consumer');
  assert.deepEqual(run.used_artifacts,fixture.used_artifacts);
  assert.equal(run.used_artifacts.length,2); // Download failure still records a real input.
  assert(run.used_artifacts.every(item=>item.artifact_id===fixture.artifact_id));
  assert.equal(fixture.used_artifact_count,1);
  assert(fixture.download_verified);
});
