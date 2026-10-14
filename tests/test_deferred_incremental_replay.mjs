/** Compare actual materialized SDK events after restart with the browser. */
import test from 'node:test';
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';
import {normalizeEvents} from '../web/model.mjs';

test('offline increment retains one base and matching config after process crash',()=>{
  const fixture=JSON.parse(execFileSync(process.env.BACKPROP_PYTHON || 'python3',
    ['-B','tests/deferred_incremental_replay_fixture.py'],{encoding:'utf8',env:{...process.env,PYTHONPATH:'sdk'}}));
  const view=normalizeEvents([...fixture.events].reverse().concat(fixture.events));
  assert.deepEqual(view.issues,[]);
  const run=view.runs.find(run=>run.id==='offline');
  assert.deepEqual(JSON.parse(JSON.stringify(run.config)),fixture.config);assert.equal(run.state,fixture.state);
  assert.equal(run.artifacts.length,1);assert.equal(run.artifacts[0].version,fixture.version);
  assert.deepEqual(run.artifacts[0].files.map(file=>file.name).sort(),fixture.files);
  assert(fixture.recovered_after_hard_exit && fixture.retry_records_identical && fixture.original_journal_unchanged);
});
