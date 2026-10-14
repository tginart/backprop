/** Real SDK source references survive duplicated and reordered browser events. */
import test from 'node:test';
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';
import {normalizeEvents} from '../web/model.mjs';
test('media artifact origin and file identity agree in Python and browser views',()=>{
 const fixture=JSON.parse(execFileSync(process.env.BACKPROP_PYTHON||'python3',['-B','tests/media_serialization_replay_fixture.py'],{encoding:'utf8',env:{...process.env,PYTHONPATH:'sdk'}}));
 for(const snapshot of fixture.snapshots){
  const replay=normalizeEvents([...snapshot.events].reverse().concat(snapshot.events));
  assert.deepEqual(replay.issues,[]);assert.equal(replay.runs.length,1);
  const actual=replay.runs[0].artifacts.map(artifact=>({id:artifact.artifact_id,files:artifact.files.map(file=>({name:file.name,sha256:file.sha256,ref:file.ref??null,birth_artifact_id:file.birth_artifact_id??null}))}));
  const sorted=values=>values.map(value=>({...value,files:[...value.files].sort((a,b)=>a.name.localeCompare(b.name))})).sort((a,b)=>a.id.localeCompare(b.id));
  assert.deepEqual(sorted(actual),sorted(snapshot.expected));
  if(actual.length>1){
   const copied=actual.filter(artifact=>artifact.id!==fixture.source_id);
   assert.equal(copied.length,2);
   for(const artifact of copied){
    assert.equal(artifact.files.length,1);assert.equal(artifact.files[0].birth_artifact_id,fixture.source_id);
    assert(artifact.files[0].ref.startsWith(`backprop-artifact://${fixture.source_id}/`));
   }
  }
 }
});
