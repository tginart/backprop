/** Deferred distributed finalizations agree across Python and browser replay. */
import test from 'node:test';
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';
import {normalizeEvents} from '../web/model.mjs';
const fixture=JSON.parse(execFileSync(process.env.BACKPROP_PYTHON||'python3',
  ['-B','tests/distributed_artifact_replay_fixture.py'],{encoding:'utf8',env:{...process.env,PYTHONPATH:'sdk'}}));
test('distributed finalization survives a crash and leaves late parts for the next version',()=>{
  const pending=normalizeEvents(fixture.pending).runs.find(r=>r.id==='worker');
  assert.equal(pending.artifacts.length,0);assert.equal(pending.pending_artifacts.length,1);
  for(const events of [fixture.delayed,fixture.events])assert.deepEqual(normalizeEvents([...events].reverse().concat(events)).issues,[]);
  const view=normalizeEvents([...fixture.events].reverse().concat(fixture.events));
  for(const [id,expected] of Object.entries(fixture.runs)){
    const run=view.runs.find(r=>r.id===id && r.project==='p');assert.equal(run.state,expected.state);
    assert.deepEqual(JSON.parse(JSON.stringify(run.config)),expected.config);
    assert.deepEqual(run.artifacts.map(({artifact_id,version,aliases,contribution_ids})=>({artifact_id,version,aliases,contribution_ids})),expected.outputs);
  }
  assert(fixture.hard_exit_recovered && fixture.late_part_retained && fixture.unchanged_journal && fixture.file_bytes_verified);
});
test('contribution output membership is scoped and leaves unrelated parts pending',()=>{
  const original=fixture.events.find(e=>e.type==='artifact_part'&&e.run_id==='worker'), variants=[];
  for(const field of ['entity','project','name','artifact_type','distributed_id','part_id']){
    const part=structuredClone(original);part.event_id=`unrelated-${field}`;part.run_id=`unrelated-${field}`;
    if(['entity','project'].includes(field))part[field]='other';
    else if(['name','artifact_type'].includes(field))part.payload.manifest[field]='other';
    else part.payload[field]='other';
    variants.push(part);
  }
  const view=normalizeEvents([...fixture.events,...variants]);assert.deepEqual(view.issues,[]);
  for(const part of variants){
    const run=view.runs.find(r=>r.id===part.run_id);assert.equal(run.artifacts.length,0);
    assert.equal(run.pending_artifacts.length,1);
  }
});
test('conflicting completions and changed contribution files fail explicitly',()=>{
  const other=structuredClone(fixture.events.find(e=>e.type==='artifact'));
  other.event_id='conflicting-final';other.payload.artifact_id='another-artifact';
  assert.throws(()=>normalizeEvents([...fixture.events,other]),/conflicting completed artifacts/);
  const changed=structuredClone(fixture.events);
  for(const event of changed)if(event.type==='artifact')event.payload.files=event.payload.files.filter(file=>file.name!=='worker');
  assert.throws(()=>normalizeEvents(changed),/original contribution file/);
  for(const [field,value,message] of [['contribution_ids',null,/invalid contribution IDs/],['distributed_id',null,/invalid distributed group/]]){
    const invalid=structuredClone(fixture.events);
    for(const event of invalid)if(event.type==='artifact')event.payload[field]=value;
    assert.throws(()=>normalizeEvents(invalid),message);
  }
});
