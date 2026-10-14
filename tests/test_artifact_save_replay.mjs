/** Automatic saves and recovered edits agree between SDK queries and browser views. */
import test from 'node:test';
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';
import {normalizeEvents} from '../web/model.mjs';

test('automatic artifact save recovers file and empty manifests after a hard exit',()=>{
  const fixture=JSON.parse(execFileSync(process.env.BACKPROP_PYTHON || 'python3',
    ['-B','tests/artifact_save_replay_fixture.py'],{encoding:'utf8',env:{...process.env,PYTHONPATH:'sdk'}}));
  for(const events of [...fixture.delayed,fixture.events]){
    assert.deepEqual(normalizeEvents([...events].reverse().concat(events)).issues,[]);
  }
  const view=normalizeEvents([...fixture.events].reverse().concat(fixture.events));
  for(const [id,expected] of Object.entries(fixture.runs)){
    const run=view.runs.find(run=>run.id===id && run.project===expected.project);
    assert.equal(run.state,expected.state);
    assert.deepEqual(JSON.parse(JSON.stringify(run.config)),expected.config);
    assert.deepEqual(run.artifacts.map(({artifact_id,version,aliases,tags,metadata,description})=>
      ({artifact_id,version,aliases,tags,metadata,description})),expected.outputs);
  }
  assert(fixture.hard_exit_recovery && fixture.source_removed && fixture.source_journals_unchanged);
  assert(fixture.file_bytes_verified && fixture.empty_manifest_verified);
});
