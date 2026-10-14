/** Actual SDK journals retain completion gaps in the browser's real reducer. */
import test from 'node:test';
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';
import {normalizeEvents} from '../web/model.mjs';
import {incompleteWriters, completionText} from '../web/history-completeness.mjs';

const measurements = row => Object.fromEntries(Object.entries(row).filter(([key])=>!['_timestamp','_runtime'].includes(key)));

test('exports and browser agree across primary completion, missing records and retries',()=>{
 const snapshots=JSON.parse(execFileSync(process.env.BACKPROP_PYTHON||'python3',['-B','tests/history_export_replay_fixture.py'],{encoding:'utf8',env:{...process.env,PYTHONPATH:'sdk'}}));
 assert.deepEqual(snapshots.map(snapshot=>snapshot.name),['running','primary_finished','missing_record','complete']);
 for(const snapshot of snapshots){
  const replay=normalizeEvents([...snapshot.events].reverse().concat(snapshot.events));
  assert.deepEqual(replay.issues,[]); assert.equal(replay.runs.length,1);
  const run=replay.runs[0], expected=snapshot.expected;
  assert.equal(run.state,expected.state);
  assert.deepEqual(run.history.map(measurements),expected.history.map(measurements));
  assert.deepEqual(run.history_incomplete_writers,expected.incomplete);
  assert.equal(['running','pending'].includes(run.state)||run.history_incomplete_writers.length>0,expected.contains_live_data);
  if(snapshot.name==='missing_record'){
   assert.equal(run.state,'finished'); assert.equal(expected.incomplete.length,1);
   assert.equal(expected.incomplete[0].missing_records,1);
   assert.equal(completionText(expected.incomplete[0]),'1 record not visible yet');
  }
  if(snapshot.name==='complete'){
   assert.deepEqual(expected.incomplete,[]); assert.equal(run.history.length,3);
   assert.equal(new Set(run.history.map(row=>row._writer)).size,2);
   assert.deepEqual(run.history.filter(row=>row._writer_label==='rank1').map(row=>row._step),[0,1]);
  }
 }
});

test('invalid or conflicting writer sequences remain incomplete without allocating their range',()=>{
 const writers={one:{state:'finished',label:null}};
 const event=(sequence,id='one',type='writer_finish')=>({payload:{_writer:{id:'one',sequence}},event_id:id,type});
 for(const sequence of [null,-1,1.5,'0',true,2**53]){
  const result=incompleteWriters(writers,[event(sequence)]);
  assert.equal(result[0].invalid_sequence,true); assert.equal(result[0].label,'one');
  assert.equal(completionText(result[0]),'Record order needs verification');
 }
 const last=2**53-1, huge=incompleteWriters(writers,[event(last)]);
 assert.equal(huge[0].missing_records,last);
 assert.equal(incompleteWriters(writers,[event(0),event(0)]).length,0);
 assert.equal(incompleteWriters(writers,[event(0),event(0,'conflict')])[0].invalid_sequence,true);
 assert.equal(completionText(incompleteWriters(writers,[event(0,'one','history')])[0]),'Awaiting completion');
 assert.equal(completionText(), 'Complete');
});
