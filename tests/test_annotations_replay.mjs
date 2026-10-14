/** Native annotation files, class maps and images reconstruct identically. */
import test from 'node:test';
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';
import {normalizeEvents} from '../web/model.mjs';
import {boxGeometry} from '../web/media-view.mjs';

test('annotation labels and image descriptors survive duplicate and reversed events',()=>{
 const data=JSON.parse(execFileSync(process.env.BACKPROP_PYTHON || 'python3',['-B','tests/annotation_replay_fixture.py'],{encoding:'utf8',env:{...process.env,PYTHONPATH:'sdk'}}));
 const view=normalizeEvents([...data.events].reverse().concat(data.events));assert.deepEqual(view.issues,[]);
 const run=view.runs[0];for(const key of ['config','summary','state'])assert.deepEqual(JSON.parse(JSON.stringify(run[key])),data.expected[key]);
 const history=run.history.map(row=>Object.fromEntries(Object.entries(row).filter(([key])=>!['_timestamp','_runtime'].includes(key))));assert.deepEqual(history,data.history);
 const image=history[0].image;assert.equal(image.boxes.truth.class_labels['7'],'Target box');assert.equal(image.masks.prediction.class_labels['255'],'Foreground 255');
 assert.deepEqual(boxGeometry(image.boxes.truth.box_data[0],64,32),{x:16,y:8,width:32,height:16});
 for(const reference of [image,image.masks.prediction.mask])assert(Buffer.from(data.files[reference.path],'base64').subarray(0,8).equals(Buffer.from([137,80,78,71,13,10,26,10])));
});

test('independent shared annotation writers retain sibling layers and deterministic conflicts',()=>{
 const data=JSON.parse(execFileSync(process.env.BACKPROP_PYTHON || 'python3',['-B','tests/annotation_writers_fixture.py'],{encoding:'utf8',env:{...process.env,PYTHONPATH:'sdk'}}));
 const view=normalizeEvents([...data.events].reverse().concat(data.events));assert.deepEqual(view.issues,[]);
 const run=view.runs[0];assert.deepEqual(JSON.parse(JSON.stringify(run.config)),data.config);assert.equal(run.state,'finished');
 const layers=run.config._wandb['bounding_box/class_labels'];assert.equal(Object.keys(layers).length,3);
 assert.equal(layers.secondary_wandb_delimeter_truth.value['7'],'secondary');assert.equal(layers.comparison_wandb_delimeter_truth.value['7'],'secondary');
});

test('annotation labels keep literal keys and report malformed events',()=>{
 const event={schema_version:1,event_id:'one',run_id:'r',project:'p',type:'config',timestamp:'2026-10-01T00:00:00Z',payload:{annotation_labels:{type:'mask/class_labels',key:'__proto__',value:{'0':'background'}}}};
 const result=normalizeEvents([event]);assert.deepEqual(result.issues,[]);
 const labels=result.runs[0].config._wandb['mask/class_labels'];assert(Object.hasOwn(labels,'__proto__'));assert.equal(labels.__proto__.value['0'],'background');assert.equal(Object.prototype.value,undefined);
 event.payload.annotation_labels.value=null;assert(normalizeEvents([event]).issues.some(value=>value.includes('Invalid annotation label update')));
});
