/** Genuine Plotly trace fixtures retain Python-identical browser replay data. */
import test from 'node:test';
import assert from 'node:assert/strict';
import {execFileSync,spawnSync} from 'node:child_process';
import {normalizeEvents} from '../web/model.mjs';

const python=process.env.BACKPROP_PYTHON || 'python3';
const optional=spawnSync(python,['-B','-c',"import importlib.util,json;print(json.dumps(all(importlib.util.find_spec(name) is not None for name in ['plotly','numpy'])))"],{encoding:'utf8'});
assert.equal(optional.status,0,optional.stderr);
test('the complete SDK Plotly matrix survives reordered/duplicate browser replay',{skip:JSON.parse(optional.stdout)?false:'Optional genuine Plotly and NumPy runtime is unavailable'},()=> {
  const data=JSON.parse(execFileSync(process.env.BACKPROP_PYTHON || 'python3',['-B','tests/plotly_gap_replay_fixture.py'],{encoding:'utf8',env:{...process.env,PYTHONPATH:'sdk'},maxBuffer:8*1024*1024}));
  const view=normalizeEvents([...data.events].reverse().concat(data.events));
  assert.deepEqual(view.issues,[]);assert.equal(view.runs.length,1);
  // Browser replay derives time from the event's millisecond timestamp. Plotly
  // payloads and steps must agree exactly; Python's extra clock precision is
  // outside this figure-encoding contract, as in the existing HTML replay suite.
  const payloadRows=rows=>rows.map(row=>Object.fromEntries(Object.entries(row).filter(([key])=>!['_timestamp','_runtime'].includes(key))));
  assert.deepEqual(JSON.parse(JSON.stringify(payloadRows(view.runs[0].history))),payloadRows(data.history));
  for(const name of data.figure_names)assert.deepEqual(view.runs[0].history[0][name],data.values[name]);
  for(const name of data.native_legacy_names)assert.deepEqual(view.runs[0].history[0][name],data.values[name]);
  assert.equal(data.values.typed_matrix.spec.data[0].z.shape,'2, 2');
  assert.equal(data.values.scattergl.spec.data[0].x.dtype,'i4');
  assert.equal(data.values.animation.spec.frames[0].name,'next');
});
