/** Mutable class-label inputs produce immutable snapshots in both SDK and UI. */
import test from 'node:test';
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';
import {normalizeEvents} from '../web/model.mjs';

test('class labels retain step snapshots after edits and duplicate/reordered delivery', () => {
  const data=JSON.parse(execFileSync(process.env.BACKPROP_PYTHON || 'python3',
    ['-B','tests/classes_replay_fixture.py'],{encoding:'utf8',env:{...process.env,PYTHONPATH:'sdk'}}));
  const view=normalizeEvents([...data.events].reverse().concat(data.events));
  assert.deepEqual(view.issues,[]);
  const run=view.runs[0];
  for (const key of ['summary','state']) assert.deepEqual(JSON.parse(JSON.stringify(run[key])),data.expected[key]);
  const history=run.history.map(row=>Object.fromEntries(Object.entries(row).filter(([key])=>!['_timestamp','_runtime'].includes(key))));
  assert.deepEqual(history,data.history);
  assert.deepEqual(history.map(row=>row.labels.class_set[1].name),['foreground','renamed']);
  assert.ok(history.every(row=>row.labels.class_set[0].name==='background'));
});
