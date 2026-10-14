/** Real evaluation snapshots survive shuffled history and delayed file reads. */
import test from 'node:test';
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {execFileSync} from 'node:child_process';
import {normalizeEvents} from '../web/model.mjs';
import {manifestFiles,readArtifactTable,isArtifactTable} from '../web/table-explorer.mjs';
const saved=JSON.parse(execFileSync(process.env.BACKPROP_PYTHON||'python3',['-B','tests/eval_table_replay_fixture.py'],{encoding:'utf8',env:{...process.env,PYTHONPATH:'sdk'}}));
test('evaluation identities and immutable roles match SDK after duplicate reordered delivery',()=>{
 const run=normalizeEvents([...saved.events].reverse().concat(saved.events)).runs[0];
 const summary=JSON.parse(JSON.stringify(run.summary));
 for(const value of Object.values(summary)){assert.deepEqual(value._missing_rows,[]);delete value._missing_rows;}
 assert.deepEqual(summary,saved.summary);assert.equal(run.state,saved.state);
 for(const writer of ['primary','secondary']){
  assert.equal(run.summary[writer].data.length,1);assert(run.summary[writer].evaluate_call_id);
  assert.deepEqual(run.summary[writer].evaluation_scores,{score:{mean:1,count:1}});
  assert.deepEqual(run.summary[writer].evaluation_row_identity,{field:'row',start:1});
 }
 assert.notEqual(run.summary.primary.evaluate_call_id,run.summary.secondary.evaluate_call_id);
});
test('evaluation artifact rows, roles, stubs and dates survive delayed visibility',async()=>{
 for(const artifact of saved.artifacts){
  const files=manifestFiles(artifact),cache=new Map();let delayed=true;
  const read=async file=>{if(delayed)throw Error('not yet visible');return structuredClone(artifact.json[file.name]);};
  const file=[...files.values()].find(isArtifactTable);assert(file);
  await assert.rejects(()=>readArtifactTable(file.name,files,read,cache),/not yet visible/);
  delayed=false;const table=await readArtifactTable(file.name,files,read,cache);
  const expected=saved.summary[table.data[0][0]];
  for(const key of ['data','evaluation_roles','evaluation_scores','evaluation_row_identity'])assert.deepEqual(table[key],expected[key]);
 }
});

test('explicit disabled evaluation files agree with Python without showing a live process',async()=>{
 const record=saved.independent,run=normalizeEvents([...record.events].reverse().concat(record.events)).runs[0];
 assert.equal(run.state,record.state);assert.equal(run.state,'disabled');assert.deepEqual(run.history,[]);
 assert.equal(run.artifacts.length,1);assert.equal(run.artifacts[0].artifact_id,record.id);
 const files=manifestFiles(record),file=[...files.values()].find(isArtifactTable);
 const table=await readArtifactTable(file.name,files,async file=>structuredClone(record.json[file.name]));
 assert.equal(table.data[0][0],record.expected.answer);assert.equal(table.data[0][2],record.expected.score);
 const bytes=Buffer.from(record.bytes[table.data[0][1].artifact_path],'base64');
 assert.equal(createHash('sha256').update(bytes).digest('hex'),record.expected.audio_sha256);
 assert.deepEqual(table.evaluation_roles,record.expected.roles);
});
