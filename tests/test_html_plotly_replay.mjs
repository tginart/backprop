/** SDK HTML and Plotly files retain their native browser descriptors. */
import test from 'node:test';
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';
import {normalizeEvents} from '../web/model.mjs';

test('HTML and Plotly survive duplicate/reordered events with Python-identical data',()=>{
 const data=JSON.parse(execFileSync(process.env.BACKPROP_PYTHON || 'python3',['-B','tests/html_plotly_replay_fixture.py'],{encoding:'utf8',env:{...process.env,PYTHONPATH:'sdk'}}));
 const view=normalizeEvents([...data.events].reverse().concat(data.events));assert.deepEqual(view.issues,[]);
 const run=view.runs[0];for(const key of ['config','summary','state'])assert.deepEqual(JSON.parse(JSON.stringify(run[key])),data.expected[key]);
 const history=run.history.map(row=>Object.fromEntries(Object.entries(row).filter(([key])=>!['_timestamp','_runtime'].includes(key))));assert.deepEqual(history,data.history);
 assert.equal(history[0].html._type,'html');assert.equal(history[0].html.mime_type,'text/html');
 assert.equal(Buffer.from(data.html,'base64').toString(),'<h1>saved</h1>');
 assert.equal(history[0].plot._type,'plotly');assert.deepEqual(history[0].plot.spec.data[0],{type:'scatter3d',x:[1,2],y:[2,3],z:[4,5]});
});
