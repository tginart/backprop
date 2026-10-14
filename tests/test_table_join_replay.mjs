/** Python and browser joins must select identical persisted rows and media. */
import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';
import {readArtifactTable,manifestFiles} from '../web/table-explorer.mjs';
import {tableJoinKey} from '../web/table-join-key.mjs';
const root=fileURLToPath(new URL('../',import.meta.url));

test('cross-artifact typed joins retain all dependent media and Python key equality',async()=>{
 const result=spawnSync(process.env.BACKPROP_PYTHON || 'python3',['-B','tests/table_join_replay_fixture.py'],{cwd:root,env:{...process.env,PYTHONPATH:'sdk'},encoding:'utf8'});
 assert.equal(result.status,0,result.stderr);const fixture=JSON.parse(result.stdout),files=manifestFiles(fixture),cache=new Map();let delayed=true;
 const read=async file=>{if(delayed && file.name!=='joined.joined-table.json')throw Error('not visible yet');return fixture.json[file.name];};
 await assert.rejects(()=>readArtifactTable('joined.joined-table.json',files,read,cache),/not visible/);delayed=false;
 const table=await readArtifactTable('joined.joined-table.json',files,read,cache);
 const actual=table.data.map(row=>[row[1],row[3]].map(cell=>cell===null?null:fixture.media[cell.artifact_path]));
 assert.deepEqual(actual,fixture.expected);assert(actual.every(row=>row.every(cell=>cell===null||typeof cell==='string')));
});

test('typed keys preserve microseconds, timezone offsets and immutable containers',()=>{
 const time=value=>({_type:'timestamp',value}),tuple=data=>({_type:'python.sequence',kind:'tuple',data});
 assert.equal(tableJoinKey(true),tableJoinKey(1));assert.notEqual(tableJoinKey('1'),tableJoinKey(1));
 assert.equal(tableJoinKey(time('2024-01-01T00:00:00.000001+00:00')),tableJoinKey(time('2023-12-31T19:00:00.000001-05:00')));
 assert.notEqual(tableJoinKey(time('2024-01-01T00:00:00.000001')),tableJoinKey(time('2024-01-01T00:00:00.000002')));
 assert.notEqual(tableJoinKey(time('2024-01-01')),tableJoinKey(time('2024-01-01T00:00:00')));
 assert.notEqual(tableJoinKey(time('2024-01-01T00:00:00')),tableJoinKey(time('2024-01-01T00:00:00Z')));
 assert.equal(tableJoinKey(tuple([1,tuple([false])])),tableJoinKey(tuple([true,tuple([0])])));
 for(const value of [[],{},undefined,{_type:'python.sequence',kind:'set',data:[]},time('2024-02-30'),time('bad'),{_type:'number',value:'invalid'}])assert.throws(()=>tableJoinKey(value));
 assert.notEqual(tableJoinKey({_type:'number',value:'NaN'}),tableJoinKey({_type:'number',value:'NaN'}));
});
