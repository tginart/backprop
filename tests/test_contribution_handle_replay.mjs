/** Compare real contribution recovery with the browser's event reconstruction. */
import test from 'node:test';
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';
import {normalizeEvents} from '../web/model.mjs';
test('recovered contribution handles retain the primary union in SDK and browser',()=>{
  const fixture=JSON.parse(execFileSync(process.env.BACKPROP_PYTHON||'python3',
    ['-B','tests/contribution_handle_replay_fixture.py'],{encoding:'utf8',env:{...process.env,PYTHONPATH:'sdk'}}));
  const view=normalizeEvents([...fixture.events].reverse().concat(fixture.events));
  assert.deepEqual(view.issues,[]);
  const run=view.runs.find(r=>r.id==='shared'&&r.project==='p'),expected=fixture.expected;
  assert.equal(run.state,expected.state);assert.deepEqual(JSON.parse(JSON.stringify(run.config)),expected.config);
  assert.equal(run.artifacts.length,1);assert.equal(run.artifacts[0].artifact_id,expected.artifact_id);
  assert.equal(run.artifacts[0].version,expected.version);
  assert.deepEqual(run.artifacts[0].files.map(f=>f.name).sort(),expected.files);
  assert.deepEqual(run.pending_artifacts,[]);
  assert(fixture.hard_exit_recovered&&fixture.source_journal_unchanged&&fixture.delayed_completion_pending);
});
