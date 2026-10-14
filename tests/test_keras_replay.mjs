/** Real Keras training outputs agree in Python and browser reconstruction. */
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {normalizeEvents,flatten,keyPath} from '../web/model.mjs';
const path=process.env.BACKPROP_KERAS_REPLAY||'test-results/keras-training-replay.json';
test('Keras metrics, checkpoint files and evaluation artifacts survive duplicate replay', {skip:!fs.existsSync(path)&&'Real TensorFlow CI fixture is not available'},()=>{
 const fixture=JSON.parse(fs.readFileSync(path,'utf8')),expected=fixture.expected;
 const result=normalizeEvents([...fixture.events].reverse().concat(fixture.events));
 assert.deepEqual(result.issues,[]);assert.equal(result.runs.length,1);
 const run=result.runs[0];assert.equal(run.state,expected.state);assert.equal(run.id,expected.run_id);
 const history=run.history.map(row=>Object.fromEntries(flatten(row).map(([key,value])=>[keyPath(key).join('.'),value]).filter(([key])=>!['_timestamp','_runtime'].includes(key))));
 assert.deepEqual(history,expected.history);
 const actual=run.artifacts.map(item=>({id:item.artifact_id,files:item.files.map(file=>({name:file.name,sha256:file.sha256}))}));
 const sorted=values=>values.map(item=>({...item,files:[...item.files].sort((a,b)=>a.name.localeCompare(b.name))})).sort((a,b)=>a.id.localeCompare(b.id));
 assert.deepEqual(sorted(actual),sorted(expected.artifacts));
 assert(history.some(row=>'epoch/loss' in row));assert(history.some(row=>'batch/loss' in row));
 assert(actual.some(item=>item.id===expected.checkpoint_id&&item.files.some(file=>file.sha256===expected.checkpoint_sha256)));
});
