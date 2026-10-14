/** Actual Python artifact files must reconstruct to the same browser rows. */
import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';
import {readArtifactTable,manifestFiles,isArtifactTable,joinTables} from '../web/table-explorer.mjs';
const root=fileURLToPath(new URL('../',import.meta.url));

test('real SDK partition and duplicate-key join replay agrees in the browser',async()=>{
 const result=spawnSync(process.env.BACKPROP_PYTHON || 'python3',['-B','tests/partition_replay_fixture.py'],{cwd:root,env:{...process.env,PYTHONPATH:'sdk'},encoding:'utf8'});
 assert.equal(result.status,0,result.stderr);const fixture=JSON.parse(result.stdout),files=manifestFiles(fixture);
 const read=file=>Promise.resolve(fixture.json[file.name]);
 const partition=await readArtifactTable('left-table.partitioned-table.json',files,read);
 assert.deepEqual(partition.data,fixture.rows);assert.equal(partition.partition_parts.length,3);
 assert.deepEqual(partition.partition_parts,fixture.partition_info.partition_parts);
 assert.deepEqual(partition.partition_parts,fixture.snapshot.partition_parts);
 assert.deepEqual(await readArtifactTable('joined.joined-table.json',files,read),fixture.joined);
 assert(isArtifactTable({name:'left-table.partitioned-table.json'}));assert(isArtifactTable({name:'joined.joined-table.json'}));
});

test('delayed files retry, schema conflicts fail and references never escape their manifest',async()=>{
 const files=new Map([['part',{name:'part'}],['parts/a.table.json',{name:'parts/a.table.json'}]]),cache=new Map();let delayed=true;
 const read=async file=>{if(file.name==='part')return {_type:'partitioned-table',parts_path:'parts'};if(delayed)throw new Error('not visible');return {_type:'table',columns:['x'],data:[[1]]};};
 await assert.rejects(()=>readArtifactTable('part',files,read,cache),/not visible/);delayed=false;
 assert.deepEqual((await readArtifactTable('part',files,read,cache)).data,[[1]]);
 await assert.rejects(()=>readArtifactTable('outside',files,read),/absent/);
 files.set('parts/b.table.json',{name:'parts/b.table.json'});
 await assert.rejects(()=>readArtifactTable('part',files,async file=>file.name.endsWith('b.table.json')?{_type:'table',columns:['y'],data:[]}:read(file)),/non-matching/);
 const cycle=new Map([['a',{name:'a'}]]);
 await assert.rejects(()=>readArtifactTable('a',cycle,async()=>({_type:'joined-table',table1:'a',table2:'a',join_key:'x'})),/cycle/);
});

test('join keys follow Python scalar equality and reject compound keys',()=>{
 const left={_type:'table',columns:['key'],data:[[true],[null],['1']]},right={_type:'table',columns:['key'],data:[[1],[null],[2]]};
 assert.deepEqual(joinTables(left,right,'key').data,[[true,1],[null,null],['1',null],[null,2]]);
 assert.throws(()=>joinTables({...left,data:[[[1]]]},right,'key'),/scalar/);
 assert.throws(()=>joinTables(left,right,'absent'),/absent/);
});

test('composite or untyped partition parts fail visibly',async()=>{
 const files=new Map([['all',{name:'all'}],['parts/child',{name:'parts/child'}]]);
 for(const child of [{_type:'partitioned-table',parts_path:'other'},{_type:'joined-table',table1:'a',table2:'b'},{}]) {
  await assert.rejects(()=>readArtifactTable('all',files,async file=>file.name==='all'?{_type:'partitioned-table',parts_path:'parts'}:child),/not a Table/);
 }
});
