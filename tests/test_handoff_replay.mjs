/** Shared writers created by logger transfer reconstruct identically in the UI. */
import test from 'node:test';
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';
import {normalizeEvents} from '../web/model.mjs';

test('parent promotion, secondary finish and primary restart retain all writer results', () => {
  const data=JSON.parse(execFileSync(process.env.BACKPROP_PYTHON || 'python3',
    ['-B','tests/handoff_replay_fixture.py'],{encoding:'utf8',env:{...process.env,PYTHONPATH:'sdk'}}));
  const view=normalizeEvents([...data.events].reverse().concat(data.events));
  assert.deepEqual(view.issues,[]);
  const run=view.runs[0];
  for (const key of ['summary','config','writers','state'])
    assert.deepEqual(JSON.parse(JSON.stringify(run[key])),data.expected[key],key);
  const history=run.history.map(row=>Object.fromEntries(Object.entries(row).filter(([key])=>!['_timestamp','_runtime'].includes(key))));
  assert.deepEqual(history,data.history);
});
