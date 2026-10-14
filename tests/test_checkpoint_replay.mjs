/** Native checkpoint retries and restarts retain one immutable browser entry. */
import test from 'node:test';
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';
import {normalizeEvents} from '../web/model.mjs';

test('checkpoint retry and journal restart agree between Python and browser', () => {
  const data = JSON.parse(execFileSync(process.env.BACKPROP_PYTHON || 'python3',
    ['-B', 'tests/checkpoint_replay_fixture.py'], {encoding:'utf8', env:{...process.env,PYTHONPATH:'sdk'}}));
  assert.equal(data.artifacts.length, 1);
  assert.equal(data.artifacts[0].bytes, 'checkpoint');
  const view = normalizeEvents([...data.events].reverse().concat(data.events));
  assert.deepEqual(view.issues, []);
  assert.deepEqual(view.runs[0].artifacts.map(a => ({id:a.artifact_id,version:a.version,
    aliases:a.aliases,metadata:a.metadata})), data.artifacts.map(({bytes,...rest})=>rest));
});
