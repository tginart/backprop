/**
 * Architecture: actual fetch/JSON boundary -> stream page recovery -> visible
 * workspace events. Main callers: browser transport tests and release CI.
 * Verify truncation cannot silently advance offsets or hide unreadable records.
 */
import test from 'node:test';
import assert from 'node:assert/strict';

const config = {host:'https://foundry.example', client_id:'test', app_url:'https://backprop.example/',
  stream_dataset_rid:'ri.foundry.main.dataset.events', media_dataset_rid:'ri.foundry.main.dataset.media'};
const row = offset => ({offset:String(offset), value:{schema_version:1,event_id:String(offset),run_id:'run',
  project:'p',entity:'e',event_type:'history',timestamp:'2026-10-06T00:00:00Z',payload:'{"metrics":{"loss":0.2}}'}});
let sequence = 0;

async function fixture(callback, {end = '10'} = {}) {
  const prior = {fetch:globalThis.fetch, storage:globalThis.sessionStorage, location:globalThis.location};
  globalThis.sessionStorage = {getItem:key => key === 'backprop.oauth.session' ?
    JSON.stringify({accessToken:'fixture', expiresAt:Date.now()+3600000}) : null, setItem(){}, removeItem(){}};
  globalThis.location = {href:config.app_url, origin:'https://backprop.example'};
  globalThis.fetch = async input => {
    if (input === './config.json') return Response.json(config);
    const url = new URL(input), operation = url.pathname.split('/').at(-1);
    if (operation === 'execute') return Response.json({type:'succeeded',queryId:'test'});
    if (operation === 'getResults') return new Response('schema_version,event_id,run_id,project,entity,event_type,timestamp,payload\n');
    if (operation === 'getEndOffsets') return Response.json({'0':end});
    if (operation === 'getRecords') return callback(url);
    throw Error('Unexpected operation ' + operation);
  };
  const api = await import('../web/api.js?stream-recovery-' + ++sequence);
  await api.initialize();
  return {api, restore() {globalThis.fetch=prior.fetch;globalThis.sessionStorage=prior.storage;globalThis.location=prior.location;}};
}

for (const kind of ['body-read','incomplete-json']) {
  test('Truncated ' + kind + ' retries the same sparse position and preserves the snapshot', async () => {
    const calls = [];
    const state = await fixture(url => {
      const offset=url.searchParams.get('startOffset'), limit=Number(url.searchParams.get('limit'));
      calls.push([offset,limit]);
      if (offset === null) return Response.json([row(3)]);
      if (limit > 25) return kind === 'incomplete-json' ? new Response('[') :
        new Response(new ReadableStream({start(controller) {controller.error(new TypeError('body terminated'));}}));
      return Response.json([row(9),row(10)]);
    });
    try {
      const result=await state.api.loadWorkspace();
      assert.deepEqual(result.events.map(event=>event.event_id),['3','9']);
      assert.deepEqual(calls,[[null,1000],['4',1000],['4',500],['4',250],['4',125],['4',62],['4',31],['4',15]]);
    } finally {state.restore();}
  });
}

test('An unreadable single record still fails after bounded page reductions', async () => {
  const calls=[];
  const state=await fixture(url=>{calls.push([url.searchParams.get('startOffset'),Number(url.searchParams.get('limit'))]);return new Response('[');});
  try {
    await assert.rejects(state.api.loadWorkspace(),SyntaxError);
    assert.deepEqual(calls.map(item=>item[1]),[1000,500,250,125,62,31,15,7,3,1]);
    assert.ok(calls.every(item=>item[0]===null));
  } finally {state.restore();}
});

for (const kind of ['denied','connect','abort']) {
  test('Stream ' + kind + ' errors propagate without page reduction',async()=>{
    let calls=0;
    const state=await fixture(()=>{
      calls++;
      if(kind==='denied')return new Response(null,{status:403});
      if(kind==='connect')throw new TypeError('connection unavailable');
      return new Response(new ReadableStream({start(controller){controller.error(new DOMException('cancelled','AbortError'));}}));
    });
    try {await assert.rejects(state.api.loadWorkspace());assert.equal(calls,1);}
    finally {state.restore();}
  });
}


test('A realistic large snapshot does not require hundreds of network round trips', async () => {
    let calls = 0;
    const count = 38_900;
    const state = await fixture(url => {
        calls++;
        const start = Number(url.searchParams.get('startOffset') || 0);
        const limit = Number(url.searchParams.get('limit'));
        return Response.json(Array.from({length: Math.min(limit, count - start)}, (_, index) => row(start + index)));
    }, {end: String(count)});
    try {
        assert.equal((await state.api.loadWorkspace()).events.length, count);
        assert(calls <= 40);
    } finally {state.restore();}
});
