/** The browser and Python must retain the same inputs after a hard exit. */
import test from 'node:test';
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';
import {normalizeEvents} from '../web/model.mjs';

test('offline artifact transfer survives a hard exit and delayed producer sync',()=>{
  const fixture=JSON.parse(execFileSync(process.env.BACKPROP_PYTHON || 'python3',
    ['-B','tests/artifact_handoff_replay_fixture.py'],{encoding:'utf8',env:{...process.env,PYTHONPATH:'sdk'}}));
  for(const events of [fixture.delayed_events,fixture.events]){
    const view=normalizeEvents([...events].reverse().concat(events));
    assert.deepEqual(view.issues,[]);
    const consumer=view.runs.find(run=>run.id==='consumer');
    assert.deepEqual(consumer.used_artifacts,fixture.used_artifacts);
    assert.equal(consumer.used_artifacts.length,1);
    const producer=view.runs.find(run=>run.id==='producer');
    assert.equal(producer.artifacts.length,1);
    assert.equal(producer.artifacts[0].artifact_id,fixture.artifact_id);
  }
  assert(fixture.restarted_after_hard_exit && fixture.source_was_unsynced_until_explicit_sync && fixture.file_bytes_verified);
});
