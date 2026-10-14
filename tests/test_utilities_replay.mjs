/** Helper config and Histogram observations survive browser replay unchanged. */
import test from 'node:test';
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';
import {normalizeEvents} from '../web/model.mjs';

test('helper config and histogram snapshots agree with Python after resume and duplicate delivery',()=>{
 const fixture=JSON.parse(execFileSync(process.env.BACKPROP_PYTHON||'python3',['-B','tests/utilities_replay_fixture.py'],{encoding:'utf8',env:{...process.env,PYTHONPATH:'sdk'}}));
 const data=normalizeEvents([...fixture.events].reverse().concat(fixture.events));assert.deepEqual(data.issues,[]);
 const run=data.runs[0];for(const key of ['config','summary','state'])assert.deepEqual(JSON.parse(JSON.stringify(run[key])),fixture.expected[key]);
 const history=run.history.map(row=>Object.fromEntries(Object.entries(row).filter(([key])=>!['_timestamp','_runtime'].includes(key))));assert.deepEqual(history,fixture.history);
 assert.deepEqual({...run.config},{lr:.1,width:16});assert.deepEqual(history.map(row=>row.weights.values),[[2,3],[4,1]]);
 for(const row of history){assert.equal(row.weights._type,'histogram');assert.deepEqual(row.weights.bins,[0,1,2]);}
});
