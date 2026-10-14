/** SDK history and browser rows retain sparse values and writer identity. */
import test from 'node:test';
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';
import {normalizeEvents, flatten, keyPath} from '../web/model.mjs';

// Browser plots preserve nested values and escaped paths; the public SDK
// exposes their dotted-column view. Compare values after this explicit mapping.
const columns = row => Object.fromEntries(flatten(row).map(([key, value])=>[keyPath(key).join('.'), value]));
const measurements = row => Object.fromEntries(Object.entries(columns(row)).filter(([key])=>!['_timestamp','_runtime'].includes(key)));

test('sampled history inputs agree after delayed visibility, retries, resume and shared writers',()=>{
 const snapshots=JSON.parse(execFileSync(process.env.BACKPROP_PYTHON||'python3',['-B','tests/public_history_replay_fixture.py'],{encoding:'utf8',env:{...process.env,PYTHONPATH:'sdk'}}));
 for(const snapshot of snapshots){
  const replay=normalizeEvents([...snapshot.events].reverse().concat(snapshot.events));
  assert.deepEqual(replay.issues,[]);
  for(const run of replay.runs){
   const expected=snapshot.expected[run.id];assert.equal(run.state,expected.state);
   assert.deepEqual(run.history.map(measurements),expected.history.map(measurements));
   assert.deepEqual(run.system.map(measurements),expected.system.map(measurements));
   for(let i=0;i<run.system.length;i++){
    assert(Math.abs(run.system[i]._timestamp-expected.system[i]._timestamp)<1e-6);
    assert(Math.abs(run.system[i]._runtime-expected.system[i]._runtime)<1e-6);
   }
   const points=run.history.filter(row=>Object.hasOwn(row,'epoch')&&Object.hasOwn(row,'loss')).map(row=>({epoch:row.epoch,loss:row.loss}));
   const endpoints=points.length>2?[points[0],points.at(-1)]:points;
   assert.deepEqual(endpoints,expected.selected);
   if(run.id==='shared'){
    assert.equal(new Set(run.system.map(row=>row._writer)).size,2);
    assert.deepEqual(new Set(run.system.map(row=>row._writer_label)),new Set(['rank0','rank1']));
    assert.deepEqual(run.system.map(row=>row._step),[0,0]);
   }
  }
 }
});
