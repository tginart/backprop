/** Actual Python writers -> shuffled/duplicated events -> browser replay. */
import test from 'node:test';
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';
import {normalizeEvents} from '../web/model.mjs';
test('Python and browser agree on shared events, public edits and completion',()=>{
 const data=JSON.parse(execFileSync(process.env.BACKPROP_PYTHON || 'python3',['-B','tests/expansion_replay_fixture.py'],{encoding:'utf8',env:{...process.env,PYTHONPATH:'sdk'}}));
 const run=normalizeEvents([...data.events].reverse().concat(data.events)).runs[0];
 for(const key of ['config','summary','state','writers'])assert.deepEqual(JSON.parse(JSON.stringify(run[key])),data.expected[key],key);
 const history=run.history.map(row=>Object.fromEntries(Object.entries(row).filter(([key])=>!['_timestamp','_runtime'].includes(key))));
 assert.deepEqual(history,data.history);
 assert.deepEqual(run.artifacts.map(a=>({id:a.artifact_id,aliases:a.aliases,tags:a.tags})),data.artifacts);
 assert.deepEqual(run.pending_artifacts.map(p=>p.part_id),data.pending);
});
test('microseconds, timezone offsets and equal timestamps have canonical ordering',()=>{
 const base={entity:'e',project:'p',run_id:'r',type:'config'};
 const rows=[{...base,event_id:'z',timestamp:'2026-01-01T00:00:00.000001Z',payload:{values:{x:1}}},
 {...base,event_id:'a',timestamp:'2025-12-31T16:00:00.000002-08:00',payload:{values:{x:2}}},
 {...base,event_id:'b',timestamp:'2026-01-01T00:00:00.000002Z',payload:{values:{x:3}}}];
 assert.equal(normalizeEvents(rows.reverse()).runs[0].config.x,3);
 assert.throws(()=>normalizeEvents([rows[0],{...rows[0],payload:{values:{x:9}}}]),/Conflicting records/);
});

test('retry identity ignores object member order, including project events',()=>{
 const first={entity:'e',project:'p',run_id:'r',type:'config',event_id:'same',timestamp:'2026-01-01T00:00:00Z',payload:{values:{x:1,y:2}}};
 const duplicate={...first,payload:{values:{y:2,x:1}}};
 assert.equal(normalizeEvents([first,duplicate]).runs[0].eventCount,1);
 const project={...first,type:'project_create'};
 assert.throws(()=>normalizeEvents([project,{...project,payload:{name:'changed'}}]),/Conflicting records/);
});
