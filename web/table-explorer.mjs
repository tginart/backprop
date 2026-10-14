import {decodePublicTable} from './public-table.mjs';
/**
 * Architecture: immutable manifest / logged snapshot -> scoped table graph ->
 * exact row lookup -> dialog navigation. File reads use the existing auth API;
 * logged paths never become fetch URLs. The app supplies shared table/media UI.
 * Main callers: artifact file actions and linked cells in logged tables.
 * Why this exists: artifact references are manifest-local names, while history
 * references are table IDs. Neither may resolve against another run/version.
 */
import { escapeHtml as e } from './model.mjs';
import { tableJoinKey } from './table-join-key.mjs';

export const isArtifactTable = file => typeof file?.name === 'string' && ['.table.json','.eval-table.json','.partitioned-table.json','.joined-table.json'].some(suffix=>file.name.endsWith(suffix));

export function validateTable(value) {
  if (value?._type !== 'table' || !Array.isArray(value.columns) || !Array.isArray(value.data) ||
      value.columns.some(column => typeof column !== 'string' && !Number.isSafeInteger(column)) ||
      value.data.some(row => !Array.isArray(row) || row.length !== value.columns.length)) {
    throw new Error('This file does not contain a valid Table snapshot. Download it to inspect the original data.');
  }
  return value;
}

/** Index exact manifest names; duplicate names cannot select an arbitrary file.
 * Main callers: artifact loading and recursive media binding.
 */
export function manifestFiles(artifact) {
  const entries = new Map();
  for (const file of artifact?.files || []) {
    if (typeof file.name !== 'string' || !file.name) continue;
    if (entries.has(file.name)) throw new Error(`Artifact has duplicate file names: ${file.name}`);
    entries.set(file.name, file);
  }
  return entries;
}

/** Resolve artifact media through the exact manifest, including nested masks.
 * Main callers: artifact Table JSON loading. Preserve captions/annotations but
 * take transport identity only from the manifest, never the object's payload.
 */
export function bindArtifactMedia(value, files) {
  if (Array.isArray(value)) return value.map(item => bindArtifactMedia(item, files));
  if (!value || typeof value !== 'object') return value;
  const publicType={'html-file':'html','image-file':'image','audio-file':'audio','video-file':'video'}[value._type];
  if(publicType)value={...value,_type:publicType};
  const result = Object.fromEntries(Object.entries(value).map(([key, item]) => [key, bindArtifactMedia(item, files)]));
  if(value._type==='plotly' && value.resources) {
    function bind(resources) {return resources.map(resource=> {
      const name=resource.file?.artifact_path || resource.file?.path,file=files.get(name);
      if(!file)throw new Error(`Plotly resource is absent from this artifact: ${String(name || '(no path)')}`);
      return {...resource,file:{...file,mime_type:file.mime_type || resource.file.mime_type},...(resource.resources?{resources:bind(resource.resources)}:{})};
    });}
    try{return {...result,resources:bind(value.resources)};}catch(error){return {...result,_load_error:error.message};}
  }
  if (['image', 'audio', 'video', 'html', 'mask', 'graph', 'graph-file'].includes(value._type)) {
    const name = value.artifact_path || value.path, file = files.get(name);
    if (!file) return { ...result, _load_error: `Media file is absent from this artifact: ${String(name || '(no path)')}` };
    // Remove untrusted routing fields before binding authoritative file fields.
    const { dataset_rid, path, branch, external, file: ignored, ...metadata } = result;
    return { ...metadata, ...file, _type: value._type === 'mask' ? 'image' : value._type,
      mime_type: file.mime_type || value.mime_type, artifact_path: name };
  }
  return result;
}

/** Scope logged relations to the exact enclosing snapshot graph.
 * Main callers: linked-cell rendering/activation. No global table registry is
 * consulted. Conflicting snapshots with one ID are reported as ambiguous.
 */
export function snapshotTables(root) {
  const tables = new Map(), seen = new Set();
  function visit(table, declaredId) {
    if (!table || typeof table !== 'object' || seen.has(table)) return;
    seen.add(table);
    const id = declaredId || table.table_id;
    if (id) {
      const prior = tables.get(id);
      tables.set(id, tables.has(id) && (prior === null || prior !== table && JSON.stringify(prior) !== JSON.stringify(table)) ? null : table);
    }
    for (const [key, target] of Object.entries(table.related_tables || {})) visit(target, key);
  }
  visit(root);
  return tables;
}

export function tableRelation(table, column, row, context) {
  const schema = table.column_types?.[column];
  if (!['foreignKey', 'foreignIndex'].includes(schema?.wb_type)) return null;
  return resolveRelation(schema, table.data?.[row]?.[column], context);
}

function resolveRelation(schema, value, context) {
  const title = schema.table_path || schema.table_id || 'Linked table';
  const result = { schema, value, title };
  if (value === undefined || value === null) return { ...result, error: 'No linked value in this row.' };
  if (schema.table_path) {
    if (!context.artifact || !context.files?.has(schema.table_path)) return { ...result, error: 'Linked table is absent from this artifact manifest.' };
    return { ...result, path: schema.table_path };
  }
  const target = context.tables?.get(schema.table_id);
  if (!target) return { ...result, error: context.tables?.has(schema.table_id) ? 'Linked table identity is ambiguous in this snapshot.' : 'Linked table was not included in this snapshot.' };
  return { ...result, target };
}

/** Pair recursive declared schemas with exact cell paths, never flattened keys.
 * Main callers: table grids, expanded row inspection and link activation.
 * Union branches produce separately labeled links: stored scalar values cannot
 * identify a unique branch when multiple referenced tables use the same key.
 */
export function nestedTableRelations(table,column,row,context) {
  const found=[];
  function visit(schema,value,path,choices,depth=0) {
    if(!schema || typeof schema!=='object')return;
    if(depth>50){found.push({value,cellPath:path,choice:choices.join(' · '),title:'Nested relation',error:'This relationship schema is too deeply nested to navigate. The full saved schema remains available.'});return;}
    if(['foreignKey','foreignIndex'].includes(schema.wb_type)) {
      if(path.length || choices.length) found.push({...resolveRelation(schema,value,context),cellPath:path,choice:choices.join(' · ')});
      return;
    }
    if(Array.isArray(schema.options))schema.options.forEach((option,index)=>visit(option,value,path,[...choices,`Option ${index+1}`],depth+1));
    if(schema.fields && value && typeof value==='object' && !Array.isArray(value))for(const [key,child] of Object.entries(schema.fields)) {
      if(Object.hasOwn(value,key))visit(child,value[key],[...path,key],choices,depth+1);
    }
    if(schema.item_type && Array.isArray(value))value.forEach((item,index)=>visit(schema.item_type,item,[...path,index],choices,depth+1));
    if(schema.column_types && value?._type==='table' && Array.isArray(value.columns) && Array.isArray(value.data))for(const [name,child] of Object.entries(schema.column_types)) {
      const matches=value.columns.map((column,index)=>({column,index})).filter(item=>String(item.column)===name);
      if(matches.length>1){found.push({value:name,cellPath:[...path,'column_types',name],choice:choices.join(' · '),title:'Nested table column',error:'The saved schema column name matches multiple original columns. This link is ambiguous.'});continue;}
      const index=matches[0]?.index;if(index===undefined)continue;
      value.data.forEach((cells,rowIndex)=>visit(child,cells[index],[...path,'data',rowIndex,index],choices,depth+1));
    }
  }
  visit(table.column_types?.[column],table.data?.[row]?.[column],[],[]);
  return found.map(relation=>({...relation,label:relation.cellPath.map(part=>typeof part==='number'?`[${part}]`:`[${JSON.stringify(part)}]`).join('') || 'Value'}));
}

/** Match original row identity, never sorted positions or coerced key values.
 * Main callers: linked-row navigation after lazy target loading.
 */
export function linkedRow(table, schema, value) {
  validateTable(table);
  if (schema.wb_type === 'foreignIndex') {
    const index=value<0 ? table.data.length+value : value;
    if (!Number.isSafeInteger(value) || index < 0 || index >= table.data.length || table._missing_rows?.includes(index)) throw new Error('The linked row is absent from this saved snapshot.');
    return index;
  }
  const column = table.columns.findIndex(name => name === schema.column);
  if (column < 0) throw new Error('The linked key column is absent from this saved snapshot.');
  let found = -1;
  for (let index = 0; index < table.data.length; index++) {
    if (table.data[index][column] !== value || table._missing_rows?.includes(index)) continue;
    if (found !== -1) throw new Error('More than one row has this key. The saved relation is ambiguous.');
    found = index;
  }
  if (found < 0) throw new Error('No row with this key exists in the saved linked table.');
  return found;
}

/** Reconstruct partition and join rows using one immutable manifest.
 * Main callers: artifact explorer and shared SDK/browser replay tests. Reads
 * cannot escape that manifest. Failed reads leave no cached promise, so delayed
 * file visibility can be retried. Cycles fail before awaiting a cached ancestor.
 */
export async function readArtifactTable(path, files, readJson, cache=new Map(), ancestors=[], readBytes) {
  if (ancestors.includes(path)) throw new Error('Artifact table references form a cycle.');
  if (cache.has(path)) return cache.get(path);
  const file=files.get(path);
  if(!file)throw new Error(`Table file is absent from this artifact manifest: ${path}`);
  const pending=(async()=>{
    const value=await readJson(file), next=[...ancestors,path];
    if(value?._type==='partitioned-table') {
      if(typeof value.parts_path!=='string')throw new Error('Partition directory must be a string.');
      const normalized=value.parts_path.split('/').filter(part=>part && part!=='.').join('/') || '.';
      const prefix=(value.parts_path.startsWith('/') ? '/' : '')+normalized+'/';
      const parts=[...files.keys()].filter(name=>name.startsWith(prefix));
      let columns=null;const data=[],metadata=[];
      for(const partPath of parts) {
        const raw=await readJson(files.get(partPath));
        if(raw?._type!=='table')throw new Error(`Partition is not a Table: ${partPath}`);
        const part=await readArtifactTable(partPath,files,async file=>file.name===partPath?raw:readJson(file),cache,next,readBytes);
        if(columns===null)columns=part.columns;
        else if(JSON.stringify(columns)!==JSON.stringify(part.columns))throw new Error('Table parts have non-matching columns.');
        metadata.push({path:partPath,row_offset:data.length,row_count:part.data.length,column_types:raw.column_types || [],...(raw.evaluation_roles?{evaluation_roles:raw.evaluation_roles}:{})});
        data.push(...part.data);
      }
      return {_type:'table',columns:columns || [],data,parts_path:value.parts_path,partition_parts:metadata};
    }
    if(value?._type==='joined-table') {
      const left=await readArtifactTable(value.table1,files,readJson,cache,next,readBytes);
      const right=await readArtifactTable(value.table2,files,readJson,cache,next,readBytes);
      return joinTables(left,right,value.join_key);
    }
    return validateTable(bindArtifactMedia(await decodePublicTable(value,files,readJson,readBytes),files));
  })().catch(error=>{cache.delete(path);throw error;});
  cache.set(path,pending);return pending;
}

/** Mirror the SDK full outer join, including duplicate matches and null keys.
 * Main callers: immutable JoinedTable reconstruction. Numeric and boolean keys
 * share Python's equality; typed dates and immutable containers retain identity.
 */
export function joinTables(left,right,joinKey) {
  validateTable(left);validateTable(right);
  const keys=typeof joinKey==='string'?[joinKey,joinKey]:joinKey;
  if(!Array.isArray(keys)||keys.length!==2)throw new Error('JoinedTable needs one key or a pair of keys.');
  const li=left.columns.indexOf(keys[0]),ri=right.columns.indexOf(keys[1]);
  if(li<0||ri<0)throw new Error('JoinedTable key column is absent.');
  const index=new Map(),data=[],matched=new Set();
  right.data.forEach((row,i)=>{const k=tableJoinKey(row[ri]);if(!index.has(k))index.set(k,[]);index.get(k).push([i,row]);});
  for(const row of left.data) {
    const found=index.get(tableJoinKey(row[li])) || [];
    if(!found.length)data.push([...row,...right.columns.map(()=>null)]);
    for(const [i,other] of found){matched.add(i);data.push([...row,...other]);}
  }
  right.data.forEach((row,i)=>{if(!matched.has(i))data.push([...left.columns.map(()=>null),...row]);});
  return {_type:'table',columns:[...left.columns.map(name=>'left.'+name),...right.columns.map(name=>'right.'+name)],data};
}

/** Build a reusable dialog with lazy reads and an explicit breadcrumb stack.
 * Main callers: app artifact/linked-cell actions. A request generation prevents
 * late network results from reopening a closed dialog or replacing another view.
 */
export function createTableExplorer({ dialog, readJson, readBytes, renderTable, renderRow, rowsFor, register, hydrate, icon, download, replace = (node, html) => { node.innerHTML = html; } }) {
  let frames = [], generation = 0, serial = 0;
  const cache = new WeakMap();
  const active = () => frames.at(-1);
  const contextFor = (artifact, root) => artifact
    ? { artifact, files: manifestFiles(artifact) }
    : { tables: snapshotTables(root) };
  async function artifactTable(context, path) {
    let entries = cache.get(context.artifact);
    if (!entries) { entries = new Map(); cache.set(context.artifact, entries); }
    return readArtifactTable(path, context.files, readJson, entries, [], readBytes);
  }
  function render({ focus = false } = {}) {
    const frame = active(); if (!frame) return;
    const artifact = frame.context.artifact;
    const crumbs = frames.map((item, index) => `<button data-explorer-crumb="${index}" ${index === frames.length - 1 ? 'disabled aria-current="page"' : ''}>${e(item.title)}${item.row !== undefined ? ` · Row ${item.row}` : ''}</button>`).join('<span aria-hidden="true">/</span>');
    let content, footer = '';
    if (frame.loading) content = '<div class="explorer-state" role="status"><span class="content-loading"><span class="loading-spinner" aria-hidden="true"></span><span>Loading table…</span></span><p>Reading the saved table.</p></div>';
    else if (frame.error) content = `<div class="explorer-state"><div class="notice error" role="alert">${e(frame.error)}</div>${frame.load ? '<button class="btn" data-explorer-retry>Retry</button>' : ''}</div>`;
    else {
      register(frame);
      content = frame.row === undefined ? renderTable(frame.table, frame.id, frame.context) : renderRow(frame.table, frame.id, frame.row, frame.context);
      // Artifact files contain one increment; unlike history replay, this
      // reader never follows prior artifact references to rebuild the table.
      if (frame.table.partition_parts) content = `<p class="dim partition-info" role="note">${frame.table.partition_parts.length} saved parts · ${frame.table.data.length} rows, in saved part order.</p>${content}`;
      if (artifact && frame.table.log_mode === 'INCREMENTAL' && !frame.table._public_increment_reconstruction) content = `<div class="notice" role="note">Current increment only · Rows stored in this file: ${frame.table.data.length}. Previous increments are not reconstructed in this view. Download JSON preserves the saved increment references.</div>${content}`;
      if (frame.row !== undefined) {
        let rows = rowsFor(frame.table, frame.id); if (!rows.some(item => item.index === frame.row)) rows = frame.table.data.map((cells, index) => ({ cells, index }));
        const position = rows.findIndex(item => item.index === frame.row);
        footer = `<span class="dim">${position + 1} of ${rows.length} rows</span><span class="grow"></span><button class="btn" data-explorer-row="${rows[position - 1]?.index ?? 0}" ${position <= 0 ? 'disabled' : ''}>Previous row</button><button class="btn" data-explorer-row="${rows[position + 1]?.index ?? 0}" ${position >= rows.length - 1 ? 'disabled' : ''}>Next row</button>`;
      } else footer = `<span class="dim">Read-only ${artifact ? 'file' : 'snapshot'} · Click a row to inspect full text and media.</span><span class="grow"></span><button class="btn small" data-explorer-json>Download JSON</button>`;
    }
    dialog.className = 'table-explorer';
    replace(dialog, `<header class="dialog-header"><div><div class="eyebrow">${artifact ? 'Artifact table' : frame.kind === 'snapshot' ? 'Saved table' : 'Linked table'}${artifact ? ` · ${e(artifact.name)}` : ''}</div><h2>${e(frame.title)}${frame.row !== undefined ? ` <span class="dim">/ Row ${frame.row}</span>` : ''}</h2>${artifact ? `<div class="explorer-version mono">${e(artifact.version ?? artifact.digest ?? 'Saved manifest')}</div>` : ''}</div><button class="icon-btn" data-explorer-close aria-label="Close table explorer">${icon('close')}</button></header><nav class="table-breadcrumbs" aria-label="Table navigation">${frames.length > 1 ? `<button data-explorer-crumb="${frames.length - 2}" aria-label="Back to previous table">${icon('left')} Back</button>` : ''}${crumbs}</nav>${frame.table && !frame.error && !frame.loading && frame.row !== undefined ? '<div class="explorer-row-toolbar"><button class="btn small" data-explorer-all>View all rows</button></div>' : ''}<div class="dialog-body" tabindex="-1">${content}</div><footer class="dialog-footer">${footer}</footer>`);
    if (!dialog.open) dialog.showModal();
    hydrate(dialog);
    if (focus) dialog.querySelector('.dialog-body').focus({ preventScroll: true });
  }
  async function load(frame) {
    const token = ++generation;
    frame.loading = true; frame.error = ''; render();
    try {
      const table = await frame.load();
      if (token !== generation || !dialog.open || frame !== active()) return;
      frame.table = table;
      if (frame.relation) frame.row = linkedRow(table, frame.relation.schema, frame.relation.value);
    } catch (error) { if (token !== generation || !dialog.open || frame !== active()) return; frame.error = error.message || 'Could not load the saved table.'; }
    frame.loading = false; render({ focus: true });
  }
  dialog.addEventListener('close', () => { generation++; frames = []; });
  dialog.addEventListener('click', event => {
    const target = event.target.closest('button'); if (!target) return;
    if (target.hasAttribute('data-explorer-close')) dialog.close();
    else if (target.hasAttribute('data-explorer-crumb')) { generation++; frames = frames.slice(0, Number(target.dataset.explorerCrumb) + 1); render({ focus: true }); }
    else if (target.hasAttribute('data-explorer-row')) { active().row = Number(target.dataset.explorerRow); render({ focus: true }); }
    else if (target.hasAttribute('data-explorer-all')) { delete active().row; render({ focus: true }); }
    else if (target.hasAttribute('data-explorer-retry')) load(active());
    else if (target.hasAttribute('data-explorer-json')) download(active().table, active().title);
  });
  return {
    has: id => dialog.open && active()?.id === id,
    render,
    openRow(id, row) { if (this.has(id)) { active().row = row; render({ focus: true }); return true; } return false; },
    openChild(frame) { frames.push(frame); render({ focus: true }); },
    openArtifact(artifact, path) {
      const context = contextFor(artifact), frame = { id: `artifact-table-${++serial}`, title: path, context };
      frame.load = () => artifactTable(context, path); frames = [frame]; return load(frame);
    },
    openSnapshot(table, title) {
      const frame = { id: `saved-table-${++serial}`, title, kind: 'snapshot', context: contextFor(null, table) };
      frame.load = async () => validateTable(table); frames = [frame]; return load(frame);
    },
    follow(source, row, column, nestedIndex) {
      if (!dialog.open || active()?.id !== source.id) frames = [{ ...source, row }];
      const relation = nestedIndex === undefined ? tableRelation(source.table, column, row, source.context) : nestedTableRelations(source.table,column,row,source.context)[nestedIndex];
      if (!relation) return;
      const frame = { id: `linked-table-${++serial}`, title: relation.title, context: source.context, relation };
      frame.load = async () => { if (relation.error) throw new Error(relation.error); return relation.target || artifactTable(source.context, relation.path); };
      frames.push(frame); return load(frame);
    },
  };
}
