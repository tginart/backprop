/** Verify real competing-process artifacts in the browser's event reducer. */
import test from 'node:test';
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';
import {normalizeEvents} from '../web/model.mjs';

test('private journals with different base views converge after a claim-time crash',()=>{
  const fixture=JSON.parse(execFileSync(process.env.BACKPROP_PYTHON || 'python3',
    ['-B','tests/shared_artifact_resolution_replay_fixture.py'],{encoding:'utf8',env:{...process.env,PYTHONPATH:'sdk'}}));
  const view=normalizeEvents([...fixture.events].reverse().concat(fixture.events));
  assert.deepEqual(view.issues,[]);
  for(const [id,expected] of Object.entries(fixture.runs)){
    const run=view.runs.find(run=>run.id===id);
    assert.equal(run.state,expected.state);
    assert.deepEqual(JSON.parse(JSON.stringify(run.config)),expected.config);
    assert.equal(run.artifacts.length,1);
    assert.equal(run.artifacts[0].version,fixture.version);
    assert.deepEqual(run.artifacts[0].files.map(file=>file.name).sort(),fixture.files);
  }
  assert(fixture.different_candidates && fixture.recovered_after_claim_exit && fixture.source_removed && fixture.unchanged_journals);
});
