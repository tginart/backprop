/** Control state contracts are pure; browser QA exercises consent and writes.
 * Main caller: node --test tests/test_frontend_run_control.mjs.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import {normalizeEvents} from '../web/model.mjs';
import {stopPhase} from '../web/run-control.mjs';
const event=(id,type,payload,timestamp='2026-10-03T00:00:01Z')=>({schema_version:1,event_id:id,run_id:'run',entity:'team',project:'experiment',type,payload,timestamp});
const start=event('start','run_start',{control:{instance_id:'instance-a',stop_supported:true,poll_interval_seconds:15}});

test('stop acknowledgments survive client clock skew without marking the run finished',()=>{
  const model=normalizeEvents([start,event('ack','run_stop_ack',{instance_id:'instance-a',request_id:'request',status:'accepted'},'2026-10-03T00:00:02Z'),event('request','run_stop_request',{instance_id:'instance-a',action:'stop'},'2026-10-03T01:00:00Z')]);
  const run=model.runs[0];
  assert.deepEqual(model.issues,[]); assert.equal(run.state,'running');
  assert.equal(run.stop_requests.length,1); assert.equal(run.stop_requests[0].status,'accepted');
  assert.equal(stopPhase(run,'instance-a').kind,'accepted');
});

test('resume drops older-instance controls and acknowledgment failure remains explicit',()=>{
  const request=event('request','run_stop_request',{instance_id:'instance-a',action:'stop'});
  const failed=event('failed','run_stop_ack',{instance_id:'instance-a',request_id:'request',status:'failed',error:'ValueError'});
  const run=normalizeEvents([start,request,failed,event('accepted','run_stop_ack',{instance_id:'instance-a',request_id:'request',status:'accepted'})]).runs[0];
  assert.equal(stopPhase(run,'instance-a').kind,'failed');
  const restarted=normalizeEvents([start,request,event('resume','run_start',{control:{instance_id:'instance-b',stop_supported:true}},'2026-10-03T00:00:03Z'),failed]).runs[0];
  assert.deepEqual(restarted.stop_requests,[]);
  assert.equal(stopPhase(restarted,'instance-a').kind,'changed');
  assert.equal(stopPhase(restarted,'instance-b').kind,'confirm');
  assert.equal(normalizeEvents([start,event('offline','run_start',{},'2026-10-03T00:00:04Z')]).runs[0].control,null);
});

test('uncertain delivery, confirmed delivery, absent capabilities, and terminal state differ',()=>{
  const run=normalizeEvents([start]).runs[0];
  assert.equal(stopPhase(run,'instance-a',{requestId:'request',sent:false}).kind,'uncertain');
  assert.equal(stopPhase(run,'instance-a',{requestId:'request',sent:true}).kind,'requested');
  assert.equal(stopPhase({...run,state:'finished'},'instance-a').kind,'finished');
  assert.equal(stopPhase({...run,control:null},undefined).kind,'unavailable');
});
