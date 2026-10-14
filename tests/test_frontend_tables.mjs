/** Immutable Table contracts: exact relation identity, manifest-local media,
 * malformed targets, and replay of nested incremental snapshots. Browser QA
 * exercises loading, navigation, grid controls and media in real dialogs.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { manifestFiles, bindArtifactMedia, validateTable, snapshotTables, tableRelation, linkedRow, isArtifactTable, createTableExplorer } from '../web/table-explorer.mjs';
import { normalizeEvents, tableAtStep } from '../web/model.mjs';

const target = { _type:'table', table_id:'target', columns:['id','name'], data:[['a','Alpha'],['b','Beta']] };
const schema = { wb_type:'foreignKey',table_id:'target',column:'id' };
const source = { _type:'table',columns:['key'],data:[['b']],column_types:[schema],related_tables:{target} };

test('linked keys use the declared column and strict scalar identity, independent of grid ordering', () => {
  const context = { tables:snapshotTables(source) }, relation = tableRelation(source,0,0,context);
  assert.equal(relation.target, target);
  assert.equal(linkedRow(relation.target,relation.schema,relation.value),1);
  assert.throws(() => linkedRow(target,{...schema,column:'missing'},'b'),/column is absent/);
  assert.throws(() => linkedRow({...target,data:[['1','string']]},schema,1),/No row/);
  assert.throws(() => linkedRow({...target,data:[['b','one'],['b','two']]},schema,'b'),/ambiguous/);
});

test('foreign indices preserve Python negative indexing and reject missing or invalid rows', () => {
  const index = { wb_type:'foreignIndex' };
  assert.equal(linkedRow(target,index,0),0);
  assert.equal(linkedRow(target,index,-1),1);assert.equal(linkedRow(target,index,-2),0);
  assert.throws(()=>linkedRow({...target,_missing_rows:[1]},index,-1),/absent/);
  for (const value of [-3,2,.5,'1']) assert.throws(() => linkedRow(target,index,value),/absent/);
  assert.throws(() => linkedRow({...target,_missing_rows:[0]},index,0),/absent/);
});

test('relations stay within their snapshot graph and missing/ambiguous identities are explicit', () => {
  assert.match(tableRelation(source,0,0,{tables:new Map()}).error,/not included/);
  const later = {...source,related_tables:{target:{...target,data:[['b','Later snapshot']]}}};
  assert.equal(tableRelation(later,0,0,{tables:snapshotTables(later)}).target.data[0][1],'Later snapshot');
  assert.equal(tableRelation(source,0,0,{tables:snapshotTables(source)}).target.data[1][1],'Beta');
  const nested = {...source, related_tables:{target,container:{related_tables:{target:{...target,data:[]}}}}};
  assert.match(tableRelation(nested,0,0,{tables:snapshotTables(nested)}).error,/ambiguous/);
  assert.match(tableRelation({...source,data:[[null]]},0,0,{tables:snapshotTables(source)}).error,/No linked value/);
});

test('artifact media and table paths bind only to exact entries of the selected manifest', () => {
  const file = {name:'media/img.png',dataset_rid:'allowed',path:'blobs/version1',sha256:'verified',size:20};
  const artifact = {files:[file,{name:'target.table.json',path:'blobs/table'}]}, files=manifestFiles(artifact);
  const media = {_type:'image',artifact_path:'media/img.png',path:'https://bad.invalid',dataset_rid:'bad',branch:'attack',file:{path:'bad'},caption:'<img onerror=alert(1)>',masks:{layer:{mask:{_type:'image',artifact_path:'media/img.png'}}}};
  const bound=bindArtifactMedia(media,files);
  assert.equal(bound.path,'blobs/version1'); assert.equal(bound.dataset_rid,'allowed'); assert.equal(bound.file,undefined); assert.equal(bound.branch,undefined);
  assert.equal(bound.caption,media.caption); assert.equal(bound.masks.layer.mask.path,'blobs/version1');
  assert.equal(bindArtifactMedia(media,manifestFiles({files:[{...file,path:'blobs/version2'}]})).path,'blobs/version2');
  assert.match(bindArtifactMedia({...media,artifact_path:'absent'},files)._load_error,/absent/);
  const table={...source,column_types:[{wb_type:'foreignKey',column:'id',table_path:'target.table.json'}]};
  assert.equal(tableRelation(table,0,0,{artifact,files}).path,'target.table.json');
  assert.match(tableRelation(table,0,0,{artifact,files:new Map()}).error,/absent/);
  assert.throws(()=>manifestFiles({files:[file,file]}),/duplicate/);
  assert(isArtifactTable({name:'results.table.json'})); assert(!isArtifactTable({name:'results.json'}));
});

test('malformed Table files fail clearly without interpreting arbitrary content', () => {
  for (const value of [{}, {_type:'table',columns:['a'],data:[[1,2]]}, {_type:'table',columns:[{}],data:[]}]) assert.throws(()=>validateTable(value),/valid Table/);
  assert.equal(validateTable({_type:'table',columns:[],data:[]}).data.length,0);
});

test('artifact file inspection labels incremental rows and preserves prior references in JSON downloads', async () => {
  const handlers = new Map(), downloads = [], reads = [];
  const dialog = { open:false, innerHTML:'', addEventListener:(name,callback)=>handlers.set(name,callback), showModal(){this.open=true;}, querySelector:()=>({focus(){}}) };
  const delta={_type:'table',columns:['value'],data:[['latest row']],log_mode:'INCREMENTAL',increment_num:1,previous_increments_paths:['backprop-artifact://prior-identity/first.table.json']};
  const file={name:'latest.table.json',path:'blobs/current'}, artifact={name:'incremental',version:'sha256-current',files:[file]};
  const explorer=createTableExplorer({dialog,readJson:async ref=>{reads.push(ref);return delta;},renderTable:()=>'<table></table>',renderRow:()=>'<p>Row</p>',rowsFor:()=>[],register(){},hydrate(){},icon:()=>'',download:value=>downloads.push(value)});
  await explorer.openArtifact(artifact,file.name);
  assert.match(dialog.innerHTML,/Current increment only · Rows stored in this file: 1/);
  assert.match(dialog.innerHTML,/Previous increments are not reconstructed/);
  assert.match(dialog.innerHTML,/Read-only file/);
  assert.deepEqual(reads,[file]);
  handlers.get('click')({target:{closest:()=>({hasAttribute:name=>name==='data-explorer-json'})}});
  assert.deepEqual(downloads[0],delta);
  assert.equal(explorer.openRow('artifact-table-1',0),true);
  assert.match(dialog.innerHTML,/Current increment only/);
  await explorer.openSnapshot({...delta,log_mode:'IMMUTABLE'},'Complete saved snapshot');
  assert.doesNotMatch(dialog.innerHTML,/Current increment only/);
});

test('nested related-table increments replay at the selected step and preserve missing-row markers', () => {
  const event = (step,related) => ({event_id:String(step),timestamp:`2026-01-01T00:00:0${step}Z`,run_id:'run',project:'p',entity:'e',type:'history',payload:{step,metrics:{predictions:{...source,related_tables:{target:related}}}}});
  const events=[event(0,{...target,log_mode:'INCREMENTAL',row_offset:0,data:[['a','Alpha']]}), event(1,{...target,log_mode:'INCREMENTAL',row_offset:1,data:[['b','Beta']]})];
  const run=normalizeEvents(events).runs[0];
  assert.equal(run.rich.predictions.value.related_tables.target.data.length,2);
  const old=tableAtStep(run,'predictions',0);
  assert.equal(old.related_tables.target.data.length,1);
  assert.throws(()=>linkedRow(old.related_tables.target,schema,'b'),/No row/);
  const partial=normalizeEvents([events[1]]).runs[0].rich.predictions.value.related_tables.target;
  assert.deepEqual(partial._missing_rows,[0]);
  assert.throws(()=>linkedRow(partial,{wb_type:'foreignIndex'},0),/absent/);
});

test('real SDK nested dictionary/list/nullable Union links use exact saved targets', async()=> {
 const {readFile}=await import('node:fs/promises'),{nestedTableRelations}=await import('../web/table-explorer.mjs');
 const history=JSON.parse(await readFile(new URL('./fixtures/native-nested-history.json',import.meta.url),'utf8'));
 const context={tables:snapshotTables(history)},links=nestedTableRelations(history,0,0,context);
 assert.deepEqual(links.map(link=>link.label),['["keys"][0]','["maybe"]']);
 assert.equal(links[1].choice,'Option 1');assert.equal(linkedRow(links[0].target,links[0].schema,links[0].value),0);
 assert.equal(linkedRow(links[1].target,links[1].schema,links[1].value),0);
 assert.match(nestedTableRelations(history,0,1,context)[1].error,/No linked value/);
 const artifact=JSON.parse(await readFile(new URL('./fixtures/native-nested-artifact.json',import.meta.url),'utf8'));
 const artifactContext={artifact:{version:'exact'},files:new Map([['media/tables/ref-0.table.json',{path:'original'}]])};
 const saved=nestedTableRelations(artifact,0,0,artifactContext);assert.equal(saved[0].path,'media/tables/ref-0.table.json');
 assert.match(nestedTableRelations(artifact,0,0,{...artifactContext,files:new Map()})[0].error,/absent from this artifact/);
});

test('nested path keys stay distinct and Union targets remain separate explicit choices',async()=> {
 const {nestedTableRelations}=await import('../web/table-explorer.mjs');
 const other={...target,table_id:'other',data:[['b','Other immutable target']]};
 const cell={'a.b':'b',a:{b:'a'},'1':['b']};
 const table={_type:'table',columns:['nested'],data:[[cell]],column_types:[{fields:{'a.b':schema,a:{fields:{b:schema}},'1':{item_type:{options:[schema,{...schema,table_id:'other'}]}}}}],related_tables:{target,other}};
 const links=nestedTableRelations(table,0,0,{tables:snapshotTables(table)});
 assert.deepEqual(links.map(link=>link.label),['["1"][0]','["1"][0]','["a.b"]','["a"]["b"]']);
 assert.equal(links[0].choice,'Option 1');assert.equal(links[1].choice,'Option 2');
 assert.notEqual(links[0].target,links[1].target);assert.equal(links[2].value,'b');assert.equal(links[3].value,'a');
});

test('nested TableType schema relates original data row and column positions',async()=> {
 const {nestedTableRelations}=await import('../web/table-explorer.mjs');
 const table={_type:'table',columns:['nested'],data:[[{_type:'table',columns:['ref'],data:[['b'],['a']]}]],column_types:[{wb_type:'table',column_types:{ref:schema}}],related_tables:{target}};
 const links=nestedTableRelations(table,0,0,{tables:snapshotTables(table)});
 assert.deepEqual(links.map(link=>link.cellPath),[['data',0,0],['data',1,0]]);
 assert.deepEqual(links.map(link=>linkedRow(link.target,link.schema,link.value)),[1,0]);
});

test('artifact nested-link activation reads the exact selected manifest entry and original row',async()=> {
 const {readFile}=await import('node:fs/promises');
 const table=JSON.parse(await readFile(new URL('./fixtures/native-nested-artifact.json',import.meta.url),'utf8'));
 const history=JSON.parse(await readFile(new URL('./fixtures/native-nested-history.json',import.meta.url),'utf8'));
 const target=Object.values(history.related_tables)[0],path=table.column_types[0].fields.keys.item_type.table_path;
 const files=[{name:'outer.table.json',path:'snapshot-original'},{name:path,path:'target-original'}],artifact={name:'same-name',version:'immutable-original',files};
 const dialog={open:false,innerHTML:'',addEventListener(){},showModal(){this.open=true;},querySelector:()=>({focus(){}})};
 const reads=[],frames=[];
 const explorer=createTableExplorer({dialog,readJson:async file=>{reads.push(file);return file===files[0]?table:target;},renderTable:()=>'',renderRow:()=>'',rowsFor:()=>[],register:frame=>frames.push(frame),hydrate(){},icon:()=>'',download(){}});
 await explorer.openArtifact(artifact,'outer.table.json');
 await explorer.follow(frames.at(-1),0,0,0);
 assert.deepEqual(reads,files);assert.equal(frames.at(-1).row,0);assert.equal(frames.at(-1).context.artifact,artifact);
 assert.match(dialog.innerHTML,/immutable-original/);
});
