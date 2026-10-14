import test from 'node:test';
import assert from 'node:assert/strict';
import {parseCsv, recordToEvent} from '../web/api.js';

test('Foundry CSV preserves escaped nested JSON, newlines, and Unicode', () => {
  const rows = parseCsv('\uFEFFevent_id,payload\r\nabc,"{""text"":""hi, \\""user\\""\\nλ""}"\r\n');
  assert.equal(rows[0].event_id, 'abc');
  assert.deepEqual(JSON.parse(rows[0].payload), {text: 'hi, "user"\nλ'});
  assert.deepEqual(parseCsv('id,text\n1,"first\nsecond"\n'), [{id:'1',text:'first\nsecond'}]);
});

test('Malformed CSV fails explicitly instead of dropping cells', () => {
  assert.throws(() => parseCsv('id,text\n1,"unfinished'), /incomplete/);
  assert.throws(() => parseCsv('id,text\n1,too,many\n'), /malformed/);
});

test('Stream and archived records become the same canonical event', () => {
  const row = {schema_version:'1',event_id:'event-1',run_id:'run-1',project:'demo',entity:'',
    event_type:'history',timestamp:'2026-10-03T00:00:00Z',payload:'{"step":2,"metrics":{"loss":0.2}}'};
  const event = recordToEvent(row);
  assert.equal(event.type,'history');
  assert.equal(event.payload.metrics.loss,0.2);
  assert.equal(event.schema_version,1);
  assert.throws(() => recordToEvent({...row,schema_version:2}), /unsupported/);
});

test('A waking tab recovers archived gaps and restarts from available live records', async () => {
  const originalFetch = globalThis.fetch;
  const config = {host:'https://foundry.example', client_id:'test', app_url:'https://backprop.example/',
    stream_dataset_rid:'ri.foundry.main.dataset.events', media_dataset_rid:'ri.foundry.main.dataset.media'};
  const row = (id) => ({schema_version:1,event_id:id,run_id:'run-1',project:'demo',entity:'',
    event_type:'history',timestamp:'2026-10-03T00:00:00Z',payload:'{"metrics":{"loss":0.2}}'});
  const csv = rows => [Object.keys(rows[0]), ...rows.map(Object.values)]
    .map(values => values.map(value => '"' + String(value).replaceAll('"','""') + '"').join(',')).join('\n');
  globalThis.sessionStorage = {getItem: key => key === 'backprop.oauth.session' ?
    JSON.stringify({accessToken:'test-token',expiresAt:Date.now()+3600000}) : null,
    setItem() {}, removeItem() {}};
  globalThis.location = {href:config.app_url,origin:'https://backprop.example'};
  let phase = 0, archiveQueries = 0;
  const starts = [];
  globalThis.fetch = async (input) => {
    if (input === './config.json') return Response.json(config);
    const url = new URL(input), operation = url.pathname.split('/').at(-1);
    if (operation === 'execute') { archiveQueries++; return Response.json({type:'succeeded',queryId:'test'}); }
    if (operation === 'getResults') return new Response(csv(phase === 2 ? [row('a'),row('b'),row('archived-gap')] : [row('a')]));
    if (operation === 'getEndOffsets') return Response.json({'0':phase === 2 ? '21' : String(phase+1)});
    if (operation === 'getRecords') {
      starts.push(url.searchParams.get('startOffset'));
      return Response.json([{offset:phase === 2 ? '20' : String(phase), value:row(['a','b','d'][phase])}]);
    }
    throw new Error('Unexpected test operation: ' + operation);
  };
  try {
    const api = await import('../web/api.js?archive-recovery');
    await api.initialize();
    assert.equal((await api.loadWorkspace()).events.length,1);
    phase = 1;
    assert.equal((await api.loadWorkspace()).events.length,2);
    assert.equal(archiveQueries,1);
    phase = 2;
    const recovered = await api.loadWorkspace({refreshArchive:true});
    assert.deepEqual(recovered.events.map(event => event.event_id).sort(),['a','archived-gap','b','d']);
    assert.equal(archiveQueries,2);
    assert.deepEqual(starts,[null,'1',null]);
  } finally { globalThis.fetch = originalFetch; }
});

test('Run stops require explicit write scope and publish only the fixed instance-targeted envelope', async () => {
  const config={host:'https://foundry.example',client_id:'test',app_url:'https://backprop.example/',stream_dataset_rid:'ri.foundry.main.dataset.events',media_dataset_rid:'ri.foundry.main.dataset.media',stream_view_rid:'view-1',branch:'master'};
  let write=false, status=204;
  globalThis.sessionStorage={getItem:key=>key==='backprop.oauth.session'?JSON.stringify({accessToken:'fixture',expiresAt:Date.now()+3600000,scopes:write?['api:use-streams-write']:[]}):null,setItem(){},removeItem(){}};
  globalThis.location={href:config.app_url,origin:'https://backprop.example'};
  const calls=[];
  globalThis.fetch=async (input,options)=>{
    if(input==='./config.json')return Response.json(config);
    calls.push({input,options});return new Response(null,{status});
  };
  const api=await import('../web/api.js?control-contract');
  const target={id:'run-1',project:'project',entity:'team',instanceId:'instance-a',requestId:'same-request',timestamp:'2026-10-03T00:00:00Z'};
  await api.initialize();
  await assert.rejects(api.publishRunStop(target),/Enable run controls/);assert.equal(calls.length,0);
  write=true;await api.initialize();
  const event=await api.publishRunStop(target);
  assert.equal(event.type,'run_stop_request');assert.deepEqual(event.payload,{action:'stop',instance_id:'instance-a'});
  const body=JSON.parse(calls[0].options.body),url=new URL(calls[0].input);
  assert.equal(url.searchParams.get('preview'),'true');assert.equal(url.searchParams.has('viewRid'),false);
  assert.equal(body.viewRid,'view-1');assert.equal(body.records.length,1);
  assert.equal(body.records[0].run_id,'run-1');assert.equal(body.records[0].event_id,'same-request');
  assert.equal(calls[0].options.redirect,'error');assert.equal(calls[0].options.credentials,'omit');
  status=403;
  await assert.rejects(api.publishRunStop(target),error=>error.status===403&&/write access/.test(error.message));
  assert.deepEqual(JSON.parse(calls[1].options.body),body,'Retry preserves the entire event identity');
  await assert.rejects(api.publishRunStop({...target,instanceId:''}),/incomplete/);
});

test('Control reads recover archived acknowledgments without hydrating unrelated rich values',async()=>{
  const config={host:'https://foundry.example',client_id:'test',app_url:'https://backprop.example/',stream_dataset_rid:'ri.foundry.main.dataset.events',media_dataset_rid:'ri.foundry.main.dataset.media'};
  globalThis.sessionStorage={getItem:key=>key==='backprop.oauth.session'?JSON.stringify({accessToken:'fixture',expiresAt:Date.now()+3600000}):null,setItem(){},removeItem(){}};
  globalThis.location={href:config.app_url,origin:'https://backprop.example'};
  const row=(id,type,payload)=>({schema_version:1,event_id:id,run_id:'run',entity:'team',project:'project',event_type:type,timestamp:'2026-10-03T00:00:00Z',payload:JSON.stringify(payload)});
  const rows=[row('start','run_start',{control:{instance_id:'instance',stop_supported:true}}),row('large','history',{metrics:{table:{_type:'table',external:true,path:'should-not-fetch'}}})];
  let queries=0;
  const csv=()=>[Object.keys(rows[0]),...rows.map(Object.values)].map(values=>values.map(value=>'"'+String(value).replaceAll('"','""')+'"').join(',')).join('\n');
  globalThis.fetch=async(input)=>{
    if(input==='./config.json')return Response.json(config);
    const operation=new URL(input).pathname.split('/').at(-1);
    if(operation==='execute'){queries++;return Response.json({type:'succeeded',queryId:'test'});}
    if(operation==='getResults')return new Response(csv());
    if(operation==='getEndOffsets')return Response.json({'0':'0'});
    throw new Error('Unexpected control I/O '+operation);
  };
  const api=await import('../web/api.js?control-archive');await api.initialize();
  const target={id:'run',entity:'team',project:'project'};
  assert.deepEqual((await api.loadRunControl(target)).events.map(event=>event.type),['run_start']);
  rows.push(row('ack','run_stop_ack',{instance_id:'instance',request_id:'request',status:'accepted'}));
  assert.deepEqual((await api.loadRunControl(target,{refreshArchive:true})).events.map(event=>event.type),['run_start','run_stop_ack']);
  assert.equal(queries,2);
});


test('external artifact references never request a Foundry blob',async()=> {
 const {getMediaUrl}=await import('../web/api.js');let requests=0;const prior=globalThis.fetch;globalThis.fetch=async()=>{requests++;throw Error('must not fetch');};
 try{await assert.rejects(getMediaUrl({_backprop_external_reference:true,uri:'s3://bucket/key',path:'key'}),/provider credentials/);assert.equal(requests,0);}finally{globalThis.fetch=prior;}
});

test('stalled SQL polling and cancellation are bounded, then a fresh load recovers', async () => {
 const config={host:'https://foundry.example',client_id:'synthetic',app_url:'https://backprop.example/',stream_dataset_rid:'ri.foundry.main.dataset.events',media_dataset_rid:'ri.foundry.main.dataset.media'};
 const memory=new Map([['backprop.oauth.session',JSON.stringify({accessToken:'synthetic',expiresAt:Date.now()+3600000})]]);
 globalThis.sessionStorage={getItem:k=>memory.get(k)||null,setItem:(k,v)=>memory.set(k,v),removeItem:k=>memory.delete(k)};
 globalThis.location={href:config.app_url,origin:new URL(config.app_url).origin};
 const timer=globalThis.setTimeout;let phase='stall',cancelled=0;
 globalThis.setTimeout=(fn,ms,...args)=>timer(fn,ms>=5000?10:Math.min(ms,1),...args);
 globalThis.fetch=async input=>{
   if(input==='./config.json')return Response.json(config);
   if(input.endsWith('/execute'))return Response.json({queryId:'synthetic-query',type:phase==='stall'?'running':'succeeded'});
   if(input.endsWith('/getStatus'))return new Promise(()=>{});
   if(input.endsWith('/cancel')){cancelled++;return new Promise(()=>{});}
   if(input.endsWith('/getResults'))return new Response('schema_version,event_id,run_id,event_type,timestamp,payload\n');
   if(input.includes('/getEndOffsets'))return Response.json({});
   throw new Error('Unexpected synthetic operation');
 };
 try {
   const api=await import('../web/api.js?bounded-sql');await api.initialize();
   await assert.rejects(api.loadWorkspace(),{name:'TimeoutError'});assert.equal(cancelled,1);
   phase='recovered';assert.equal((await api.loadWorkspace()).events.length,0);
   const controller=new AbortController();controller.abort();
   await assert.rejects(api.loadWorkspace({signal:controller.signal}),{name:'AbortError'});
 } finally {globalThis.setTimeout=timer;}
});

test('stalled startup configuration rejects, then initialization can retry', async()=> {
 const timer=globalThis.setTimeout;globalThis.setTimeout=(fn,ms,...args)=>timer(fn,Math.min(ms,10),...args);
 const api=await import('../web/api.js?bounded-startup');globalThis.fetch=()=>new Promise(()=>{});
 try {await assert.rejects(api.initialize(),{name:'TimeoutError'});}finally{globalThis.setTimeout=timer;}
});
