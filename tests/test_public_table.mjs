/** Public table files must reconstruct the same data in Python and the UI. */
import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';
import {readArtifactTable,manifestFiles,tableRelation,linkedRow} from '../web/table-explorer.mjs';
import {readNpz} from '../web/numpy-file.mjs';
import {decodePublicTable} from '../web/public-table.mjs';
const root=fileURLToPath(new URL('../',import.meta.url));
function fixture(){const result=spawnSync(process.env.BACKPROP_PYTHON||'python3',['-B','tests/public_table_replay_fixture.py'],{cwd:root,env:{...process.env,PYTHONPATH:'sdk'},encoding:'utf8'});assert.equal(result.status,0,result.stderr);return JSON.parse(result.stdout);}
const saved=fixture();

test('public timestamps, NumPy files, HTML and relationships match the persisted SDK read',async()=>{
 const files=manifestFiles(saved),cache=new Map();let delayed=true;
 const readJson=async file=>structuredClone(saved.json[file.name]);
 const readBytes=async file=>{if(delayed)throw Error('not yet visible');return Buffer.from(saved.bytes[file.name],'base64');};
 if(saved.expected.array)await assert.rejects(()=>readArtifactTable('public.table.json',files,readJson,cache,[],readBytes),/not yet visible/);
 delayed=false;const table=await readArtifactTable('public.table.json',files,readJson,cache,[],readBytes);
 assert.equal(new Date(table.data[0][0].value).toISOString(),new Date(saved.expected.time).toISOString());
 assert.equal(Buffer.from(saved.bytes[table.data[0][1].artifact_path],'base64').toString(),saved.expected.html);
 assert.equal(table.data[0][2],saved.expected.ref);
 const relation=tableRelation(table,2,0,{artifact:saved,files});assert(relation.path);
 const linked=await readArtifactTable(relation.path,files,readJson,cache,[],readBytes);
 assert.deepEqual(linked.data,saved.expected.linked_rows);assert.equal(linkedRow(linked,relation.schema,relation.value),0);
 if(saved.expected.array)assert.deepEqual({data:table.data[0][3].data,shape:table.data[0][3].shape},saved.expected.array);
 if(saved.expected.constructed){
  const constructed=await readArtifactTable('constructed.table.json',files,readJson,cache,[],readBytes);
  assert.deepEqual(constructed.data.map(row=>({data:row[0].data,shape:row[0].shape,dtype:row[0].dtype})),saved.expected.constructed);
 }
 const evaluation=await readArtifactTable('evaluation.table.json',files,readJson,cache,[],readBytes);
 assert.deepEqual(evaluation.evaluation_roles.score_columns,['score']);
});

test('browser reads real NumPy files in their declared shape and byte order',async t=>{
 if(!saved.arrays.length){t.skip('NumPy is not installed in this environment');return;}
 const result={};for(const record of saved.arrays){
  if(['object','structured'].includes(record.name)){await assert.rejects(()=>readNpz(Buffer.from(record.bytes,'base64'),'x'),/object|structured/);continue;}
  result[record.name]=await readNpz(Buffer.from(record.bytes,'base64'),'x');assert.deepEqual(result[record.name].shape,record.shape);
 }
 assert.deepEqual(result.float16.data,[[1.5,-2.25]]);assert.deepEqual(result.big_endian.data,[[0,1,2],[3,4,5]]);
 assert.deepEqual(result.fortran.data,[Array.from({length:3},(_,i)=>Array.from({length:4},(_,j)=>i*4+j)),Array.from({length:3},(_,i)=>Array.from({length:4},(_,j)=>12+i*4+j))]);
 assert.deepEqual(result.empty.data,[[],[]]);assert.equal(result.scalar.data,42);assert.deepEqual(result.unicode.data,['hello','界🙂','a\0b']);
 assert.deepEqual(result.unsigned.data,[{_type:'integer',value:'18446744073709551615'}]);assert.deepEqual(result.boolean.data,[true,false]);
 assert.deepEqual(result.complex.data,[{_type:'complex',real:1,imaginary:2}]);
 const corrupt=Buffer.from(saved.arrays[0].bytes,'base64');const at=corrupt.indexOf(Buffer.from([0x50,0x4b,0x01,0x02]));corrupt[at+16]^=1;
 await assert.rejects(()=>readNpz(corrupt,'x'),/checksum/);
 await assert.rejects(()=>readNpz(Buffer.from(saved.arrays[0].bytes,'base64'),'missing'),/absent/);
 await assert.rejects(()=>readNpz(new Uint8Array([1,2,3]),'x'),/absent/);
});

test('public incremental manifests use the same numbered ordering and missing-file errors',async()=>{
 const value={_type:'table',columns:['x'],data:[[2]],log_mode:'INCREMENTAL',increment_num:1,column_types:{wb_type:'typedDict',params:{type_map:{x:{wb_type:'number'}}}}};
 const files=new Map([['1-200.table.json',{name:'1-200.table.json'}],['0-100.table.json',{name:'0-100.table.json'}]]);
 const read=async file=>({...value,data:file.name.startsWith('0-')?[[1]]:[[2]]});
 assert.deepEqual((await decodePublicTable(value,files,read)).data,[[1],[2]]);
 assert.deepEqual((await decodePublicTable({...value,increment_num:null},files,read)).data,[]);
 await assert.rejects(()=>decodePublicTable(value,files,async()=>({})),/Invalid incremental/);
 const arrays={...value,log_mode:'IMMUTABLE',column_types:{wb_type:'typedDict',params:{type_map:{x:{wb_type:'ndarray',params:{serialization_path:{path:'absent.npz',key:'x'}}}}}}};
 await assert.rejects(()=>decodePublicTable(arrays,files,read),/Array file is absent/);
});
