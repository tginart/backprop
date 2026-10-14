/** Adapt public SDK table descriptors to the existing Foundry table viewer.
 * Architecture: typed dictionary schema -> manifest-scoped NumPy file reads ->
 * typed cells and native display schemas. Main caller: artifact table explorer.
 * No additional backend/storage resources or arbitrary reference URLs are used.
 */
import {readNpz} from './numpy-file.mjs';
const members=schema=>schema?.wb_type==='union'?schema.params.allowed_types:[schema];

/** Preserve the full source schema and expose the viewer's relationship fields.
 * Main caller: decodePublicTable. Custom types remain inspectable declarations;
 * the browser never loads Python handlers from stored type names.
 */
function displayType(schema){
 if(!schema||typeof schema!=='object')return schema;
 const p=schema.params||{},result={wb_type:schema.wb_type,upstream_schema:schema};
 if(schema.wb_type==='union'){
  const types=p.allowed_types||[],nonNull=types.filter(x=>x.wb_type!=='none');
  if(nonNull.length===1)return {...displayType(nonNull[0]),optional:types.length!==nonNull.length,upstream_schema:schema};
  result.options=types.map(displayType);
 }
 if(['foreignKey','foreignIndex'].includes(schema.wb_type)){result.table_path=p.table;if(schema.wb_type==='foreignKey')result.column=p.col_name;}
 if(schema.wb_type==='typedDict')result.fields=Object.fromEntries(Object.entries(p.type_map||{}).map(([key,value])=>[key,displayType(value)]));
 if(schema.wb_type==='list')result.item_type=displayType(p.element_type);
 if(p.shape)result.shape=p.shape;
 return result;
}

/** Load a complete manifest-local incremental table, as the public SDK does.
 * Main caller: decodePublicTable. Native snapshots retain their separate
 * explicit artifact-chain representation and are not passed through this path.
 */
async function rowsFor(value,files,readJson){
 if(value.log_mode!=='INCREMENTAL')return value.data;
 if(value.increment_num===undefined||value.increment_num===null)return [];
 const order=name=>{const parts=name.split('.')[0].split('-');return /^\d+$/.test(parts[0])&&(parts.length<2||/^\d+$/.test(parts[1]))?[Number(parts[0]),Number(parts[1]||0)]:[0,0];};
 const paths=[...files.keys()].filter(name=>name.endsWith('.table.json')).sort((a,b)=>{const x=order(a),y=order(b);return x[0]-y[0]||x[1]-y[1];}),rows=[];
 for(const path of paths){const delta=await readJson(files.get(path));if(!Array.isArray(delta.data))throw Error(`Invalid incremental table file: ${path}`);rows.push(...delta.data);}
 return rows;
}

export async function decodePublicTable(value,files,readJson,readBytes){
 if(value?._type!=='table'||value.column_types?.wb_type!=='typedDict')return value;
 const types=value.column_types.params?.type_map;if(!types||!Array.isArray(value.columns))throw Error('Invalid public Table column schema.');
 const arrays=new Map(),timestamps=new Set();
 for(const [name,schema] of Object.entries(types)){
  const index=value.columns.indexOf(name),choices=members(schema);
  if(choices.some(type=>type?.wb_type==='timestamp'))timestamps.add(index);
  const source=choices.filter(type=>type?.wb_type==='ndarray').at(-1)?.params?.serialization_path;
  if(source){
   const file=files.get(source.path);if(!file)throw Error(`Array file is absent from this artifact: ${source.path}`);
   if(!readBytes)throw Error('This table needs the artifact binary-file reader.');
   arrays.set(index,await readNpz(await readBytes(file),source.key));
  }
 }
 const rows=await rowsFor(value,files,readJson);
 if(!Array.isArray(rows))throw Error('Public Table data must contain rows.');
 const data=[];
 for(let index=0;index<rows.length;index++){
  if(!Array.isArray(rows[index])||rows[index].length!==value.columns.length)throw Error('Public Table row length does not match its columns.');
  const row=[];
  for(let column=0;column<rows[index].length;column++){
   let cell=rows[index][column];
   if(timestamps.has(column)&&['number','boolean'].includes(typeof cell)){
    const date=new Date(Number(cell));if(!Number.isFinite(date.valueOf()))throw Error('Invalid public Table timestamp.');cell={_type:'timestamp',value:date.toISOString()};
   }else if(arrays.has(column)){
    const array=arrays.get(column);if(!array.shape.length||index>=array.shape[0])throw Error('NumPy column has fewer rows than the table.');
    cell={_type:'ndarray',data:array.data[index],shape:array.shape.slice(1),dtype:array.dtype};
   }else if(cell?._type==='table')cell=await decodePublicTable(cell,files,readJson,readBytes);
   row.push(cell);
  }
  data.push(row);
 }
 return {...value,data,column_types:value.columns.map(name=>displayType(types[name])),public_column_types:value.column_types,
  ...(value.log_mode==='INCREMENTAL'?{_public_increment_reconstruction:true}:{})};
}
