/** Real polling callback -> synthetic Foundry transport -> browser-owned UI state.
 * Exercises unchanged and arriving data, including interactions begun in flight.
 * Uses an isolated browser, the release CSP, and no user data or credentials.
 */
import assert from 'node:assert/strict';
import {createServer} from 'node:http';
import {readFile} from 'node:fs/promises';
import {join,extname} from 'node:path';
import {fileURLToPath} from 'node:url';
import {spawnSync} from 'node:child_process';
import {workflowFoundryRecords,workflowFoundryCsv} from './fixtures/workflow-reference.mjs';
const root=fileURLToPath(new URL('../',import.meta.url));
const {chromium}=await import(process.env.PLAYWRIGHT_MODULE || 'playwright');
const host='https://foundry-test.invalid';
const config={host,client_id:'synthetic-client',stream_dataset_rid:'ri.foundry.main.dataset.scroll-events',media_dataset_rid:'ri.foundry.main.dataset.scroll-media'};
const audioBytes=Buffer.alloc(44+160000);
audioBytes.write('RIFF',0);audioBytes.writeUInt32LE(audioBytes.length-8,4);audioBytes.write('WAVEfmt ',8);
audioBytes.writeUInt32LE(16,16);audioBytes.writeUInt16LE(1,20);audioBytes.writeUInt16LE(1,22);
audioBytes.writeUInt32LE(8000,24);audioBytes.writeUInt32LE(16000,28);audioBytes.writeUInt16LE(2,32);audioBytes.writeUInt16LE(16,34);
audioBytes.write('data',36);audioBytes.writeUInt32LE(audioBytes.length-44,40);
const audioValue={_type:'audio',file:{dataset_rid:config.media_dataset_rid,path:'blobs/synthetic-scroll.wav',mime_type:'audio/wav',size:audioBytes.length}};
let sequence=0;
const events=[];
const add=(run,type,payload)=>events.push({schema_version:1,event_id:`synthetic-scroll-${sequence++}`,run_id:`scroll-${run}`,project:'scroll-synthetic-demo',entity:'demo-user',type,timestamp:new Date(Date.UTC(2026,9,7,12,0,sequence)).toISOString(),payload});
const table={_type:'table',columns:['name','description','array'],data:Array.from({length:80},(_,i)=>[`Synthetic row ${i}`,'Wide synthetic text '.repeat(15),{_type:'ndarray',dtype:'int64',shape:[3],data:[i,i+1,i+2]}])};
const vega={_type:'vega',table:{columns:['x','y'],data:[[0,1],[1,2]]},spec:{width:200,height:700,signals:[{name:'inspection',value:0,bind:{input:'range',min:0,max:10}}],data:[{name:'wandb'}],scales:[{name:'x',type:'linear',domain:[0,1],range:'width'},{name:'y',type:'linear',domain:[0,3],range:'height'}],marks:[{type:'symbol',from:{data:'wandb'},encode:{enter:{x:{scale:'x',field:'x'},y:{scale:'y',field:'y'},size:{value:80}}}}]}};
for(let run=0;run<40;run++){
 add(run,'run_start',{name:`Synthetic scroll run ${run}`,config:{seed:run},tags:['synthetic-demo']});
 for(let step=0;step<3;step++)add(run,'history',{step,metrics:{...Object.fromEntries(Array.from({length:24},(_,i)=>[`section-${Math.floor(i/8)}/metric-${i}`,i+step+run/100])),...(run===0?{'outputs/table':table,'outputs/vega':vega,'outputs/audio':audioValue}:{})}});
 if(run===0)add(run,'console',{text:Array.from({length:150},(_,i)=>`Synthetic log line ${i}`).join('\n')+'\n',stream:'stdout'});
}
const records=()=>workflowFoundryRecords(events);
const policy=spawnSync(process.env.PYTHON || 'python3',['-B','-c','import sys;sys.path.insert(0,sys.argv[1]);from deploy import apply_content_security_policy;sys.stdout.buffer.write(apply_content_security_policy(sys.stdin.buffer.read(),{"host":sys.argv[2]}))',join(root,'deploy'),host],{input:await readFile(join(root,'web/index.html')),encoding:'utf8'});
assert.equal(policy.status,0,policy.stderr);
const server=createServer(async(req,res)=>{
 try{
  const path=new URL(req.url,'http://localhost').pathname;
  const body=path==='/config.json'?JSON.stringify(config):path==='/'?policy.stdout:await readFile(join(root,'web',path));
  res.setHeader('Content-Type',path==='/'?'text/html':({'.mjs':'text/javascript','.js':'text/javascript','.css':'text/css','.svg':'image/svg+xml','.json':'application/json'}[extname(path)]||'text/plain'));
  res.end(body);
 }catch{res.writeHead(404);res.end();}
});
await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
const browser=await chromium.launch({headless:true,executablePath:process.env.CHROMIUM_EXECUTABLE});
const origin=`http://127.0.0.1:${server.address().port}`,errors=[];
const settle=page=>page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
async function open(query='view=workspace',viewport={width:1440,height:900}){
 const page=await browser.newPage({viewport});
 page.on('pageerror',e=>errors.push(e.message));
 await page.addInitScript(()=>{
  sessionStorage.setItem('backprop.oauth.session',JSON.stringify({accessToken:'synthetic-test-only',expiresAt:Date.now()+3600000}));
  const interval=window.setInterval;
  window.setInterval=(fn,ms,...args)=>{if(ms===30000){window.poll=()=>fn(...args);window.pollMs=ms;return 999999;}return interval(fn,ms,...args);};
 });
 let hold=null,reads=0;
 await page.route(`${host}/**`,async route=>{
  const url=new URL(route.request().url());
  if(url.pathname.endsWith('/execute'))return route.fulfill({json:{queryId:'synthetic-scroll',type:'succeeded'}});
  if(url.pathname.endsWith('/getResults'))return route.fulfill({body:workflowFoundryCsv(records()),contentType:'text/csv'});
  if(url.pathname.endsWith('/getEndOffsets')){reads++;if(hold)await hold;return route.fulfill({json:{'0':String(events.length)}});}
  if(url.pathname.endsWith('/getRecords')){
   const start=Number(url.searchParams.get('startOffset')||0),limit=Number(url.searchParams.get('limit')||100);
   return route.fulfill({json:records().slice(start,start+limit).map((record,i)=>({offset:String(start+i),value:record}))});
  }
  if(url.pathname.includes('/files/'))return route.fulfill({body:audioBytes,contentType:'audio/wav'});
  return route.fulfill({status:404});
 });
 await page.goto(`${origin}/?project=scroll-synthetic-demo&${query}`);
 await page.waitForFunction(()=>window.poll && document.querySelector('.main'));
 if(query.includes('view=workspace')||query.includes('tab=charts'))await page.locator('[data-vega-ready]').waitFor({state:'attached'});
 await settle(page);
 await page.evaluate(()=>{
  window.replacements=0;new MutationObserver(()=>window.replacements++).observe(document.querySelector('#app'),{childList:true});
 });
 assert.equal(await page.evaluate(()=>window.pollMs),30000);
 return {page,reads:()=>reads,hold:()=>new Promise(resolve=>{hold=new Promise(release=>resolve(()=>{release();hold=null;}));})};
}
async function poll(page){
 const before=await page.evaluate(()=>window.replacements);
 await page.evaluate(()=>window.poll());
 await page.waitForFunction(before=>window.replacements>before,before);
 await settle(page);
 assert.equal(await page.evaluate(()=>window.replacements),before+1,'A refresh renders once, after its request');
}
try{
 for(const spec of [
  {name:'desktop workspace',query:'view=workspace',selectors:['.charts-area','.run-list']},
  {name:'single-run Charts',query:'view=run&run=scroll-0&tab=charts',selectors:['.run-page']},
  {name:'single-run Logs',query:'view=run&run=scroll-0&tab=console',selectors:['.console-log']},
  {name:'mobile workspace',query:'view=workspace',viewport:{width:390,height:844},selectors:['.workspace']}
 ]){
  const {page}=await open(spec.query,spec.viewport);
  const before=await page.evaluate(selectors=>selectors.map(selector=>{const node=document.querySelector(selector);node.scrollTop=600;return {selector,top:node.scrollTop};}),spec.selectors);
  assert(before.every(item=>item.top>0),`${spec.name} must actually scroll`);
  await poll(page);
  assert.deepEqual(await page.evaluate(selectors=>selectors.map(selector=>({selector,top:document.querySelector(selector).scrollTop})),spec.selectors),before,spec.name);
  if(spec.name==='mobile workspace'){
   await page.locator('.run-name').first().dispatchEvent('click');
   await page.locator('[data-action="exit-run"]').click();
   assert.equal(await page.locator('.workspace').evaluate(node=>node.scrollTop),before[0].top,'Exit restores mobile workspace scroll');
  }
  await page.close();
 }
 const fixture=await open(),{page}=fixture;
 await page.locator('[data-state="search"]').fill('Synthetic');
 // Preserve nested X/Y scroll, open array cells, focus/caret, and Vega controls.
 await page.evaluate(()=>{
  const table=document.querySelector('.logged-table');table.scrollTop=300;table.scrollLeft=200;
  table.querySelector('details').open=true;
  const legend=document.querySelector('.panel-legend');legend.scrollTop=20;
  const input=document.querySelector('[data-state="search"]');input.focus();input.setSelectionRange(1,6,'backward');
  window.oldVega=document.querySelector('[data-vega-ready]');
  window.oldVega.closest('.logged-panel-body').scrollTop=240;
  window.oldVega.querySelector('input').value='7';window.oldVega.querySelector('input').dispatchEvent(new Event('input',{bubbles:true}));
  window.oldInput=input;
 });
 const nested=await page.evaluate(()=>({top:document.querySelector('.logged-table').scrollTop,left:document.querySelector('.logged-table').scrollLeft,legend:document.querySelector('.panel-legend').scrollTop,vega:document.querySelector('[data-vega-ready]').closest('.logged-panel-body').scrollTop}));
 assert(nested.top>0&&nested.left>0&&nested.legend>0&&nested.vega>0);
 await poll(page);
 assert.deepEqual(await page.evaluate(()=>({top:document.querySelector('.logged-table').scrollTop,left:document.querySelector('.logged-table').scrollLeft,legend:document.querySelector('.panel-legend').scrollTop,vega:document.querySelector('[data-vega-ready]').closest('.logged-panel-body').scrollTop})),nested);
 assert(await page.locator('.logged-table details').first().evaluate(node=>node.open));
 assert(await page.evaluate(()=>document.activeElement.matches('[data-state="search"]')&&document.activeElement.value==='Synthetic'&&document.activeElement.selectionStart===1&&document.activeElement.selectionEnd===6&&document.activeElement.selectionDirection==='backward'));
 assert(await page.evaluate(()=>document.querySelector('[data-vega-ready]')===window.oldVega&&window.oldVega.querySelector('input').value==='7'));
 // Existing media retention must keep both playing and paused player state.
 await page.waitForFunction(()=>document.querySelector('audio')?.readyState>=1);
 await page.locator('audio').evaluate(async node=>{window.retainedAudio=node;node.currentTime=4;node.volume=.3;node.playbackRate=1.25;await node.play();});
 await poll(page);
 const playback=await page.locator('audio').evaluate(node=>({same:node===window.retainedAudio,time:node.currentTime,paused:node.paused,rate:node.playbackRate,volume:node.volume}));
 assert(playback.same&&!playback.paused&&playback.time>=4&&playback.time<8);assert.equal(playback.rate,1.25);assert.equal(playback.volume,.3);
 await page.locator('audio').evaluate(node=>{node.pause();node.currentTime=6;});await poll(page);
 assert(await page.locator('audio').evaluate(node=>node.paused&&node.currentTime===6));
 // Do not replace controls at request start; capture the user's latest scroll.
 const release=await fixture.hold();
 const before=await page.evaluate(()=>window.replacements),reads=fixture.reads();
 await page.evaluate(()=>window.poll());
 await page.waitForFunction(()=>document.querySelector('[data-action="refresh"]').disabled);
 assert.equal(await page.evaluate(()=>window.replacements),before);
 await page.locator('.charts-area').evaluate(node=>{node.scrollTop=450;});
 // An action menu opened during the request must remain usable on completion.
 await page.locator('[data-action="visibility-menu"]').click();
 assert(await page.locator('.action-menu').evaluate(node=>node.matches(':popover-open')));
 release();
 await page.waitForFunction(()=>!document.querySelector('[data-action="refresh"]').disabled);
 await settle(page);
 assert(fixture.reads()>reads);
 assert.equal(await page.evaluate(()=>window.replacements),before);
 assert(await page.locator('.action-menu').evaluate(node=>node.matches(':popover-open')));
 await page.keyboard.press('Escape');
 await page.waitForFunction(before=>window.replacements>before,before);
 assert.equal(await page.locator('.charts-area').evaluate(node=>node.scrollTop),450);
 // Pointer resizing, text selection, and IME composition defer completion.
 for(const kind of ['pointer','composition','selection']){
  if(kind==='pointer'){const box=await page.locator('[data-sidebar-resize]').boundingBox();await page.mouse.move(box.x+box.width/2,box.y+20);await page.mouse.down();}
  await page.evaluate(kind=>{
   const input=document.querySelector('[data-state="metricSearch"]');
   if(kind==='selection'){const range=document.createRange();range.selectNodeContents(document.querySelector('.chart-title'));getSelection().removeAllRanges();getSelection().addRange(range);}
   if(kind==='composition'){input.focus();input.dispatchEvent(new CompositionEvent('compositionstart',{bubbles:true}));}
  },kind);
  const before=await page.evaluate(()=>window.replacements);
  await page.evaluate(()=>window.poll());
  await page.waitForFunction(()=>!document.querySelector('[data-action="refresh"]').disabled);
  await settle(page);
  assert.equal(await page.evaluate(()=>window.replacements),before,`${kind} survives polling`);
  if(kind==='pointer')await page.mouse.up();
  await page.evaluate(kind=>{
   if(kind==='selection')getSelection().removeAllRanges();
   if(kind==='composition')document.activeElement.dispatchEvent(new CompositionEvent('compositionend',{bubbles:true}));
  },kind);
  await page.waitForFunction(before=>window.replacements>before,before);
 }
 // Keyboard point inspection remains available until explicitly dismissed.
 const chart=page.locator('.chart-svg').first();await chart.focus();await chart.press('End');
 const beforeInspection=await page.evaluate(()=>window.replacements);
 await page.evaluate(()=>window.poll());await page.waitForFunction(()=>!document.querySelector('[data-action="refresh"]').disabled);await settle(page);
 assert.equal(await page.evaluate(()=>window.replacements),beforeInspection);
 assert(await page.locator('.chart-tooltip:popover-open').count());
 await chart.press('Escape');await page.waitForFunction(before=>window.replacements>before,beforeInspection);
 // A zoom and hidden series stay tied to their panel when new samples arrive.
 const scalar=page.locator('.chart-card:has(.chart-svg)').first();
 await scalar.locator('.legend-row').first().click();
 await scalar.locator('.chart-svg').scrollIntoViewIfNeeded();
 const bounds=await scalar.locator('.chart-svg').boundingBox();
 await page.mouse.move(bounds.x+bounds.width*.3,bounds.y+bounds.height*.4);await page.mouse.down();
 await page.mouse.move(bounds.x+bounds.width*.7,bounds.y+bounds.height*.6);await page.mouse.up();
 const zoom=await scalar.locator('.axis').textContent();
 add(0,'history',{step:3,metrics:{'section-2/arrival':42}});
 await page.mouse.move(0,0);await poll(page);
 assert.equal(await scalar.locator('.axis').textContent(),zoom);
 assert.equal(await scalar.locator('.legend-row').first().getAttribute('aria-pressed'),'false');
 await page.locator('[data-section="section-2"]').click();
 await page.getByLabel('Next panels in section-0',{exact:true}).click();
 const panelTitle=await scalar.locator('.chart-title').textContent();
 await page.mouse.move(0,0);await poll(page);
 assert(await page.locator('[data-section="section-2"]').evaluate(node=>node.classList.contains('collapsed')));
 assert.equal(await scalar.locator('.chart-title').textContent(),panelTitle);
 // Changed Vega input redraws; unchanged views retain the user's signal state.
 add(0,'history',{step:4,metrics:{'outputs/vega':{...vega,table:{columns:['x','y'],data:[[0,2],[1,3]]}}}});
 await poll(page);await page.locator('[data-vega-ready]').waitFor({state:'attached'});
 assert(await page.evaluate(()=>document.querySelector('[data-vega-ready]')!==window.oldVega));
 assert.equal(await page.locator('[data-vega-ready] input').inputValue(),'0');
 await settle(page);
 assert.equal(await page.locator('[data-vega-ready]').evaluate(node=>node.closest('.logged-panel-body').scrollTop),nested.vega,'Scroll recovers after changed chart hydration');
 await page.close();
 const runs=await open('view=runs'),runPage=runs.page;
 await runPage.locator('[data-run-select]').first().check();
 await runPage.locator('.run-visibility').first().click();
 await runPage.locator('.runs-table').evaluate(node=>{node.scrollTop=200;node.scrollLeft=100;});
 const tableScroll=await runPage.locator('.runs-table').evaluate(node=>[node.scrollTop,node.scrollLeft]);
 assert(tableScroll[0]>0);
 await poll(runPage);
 assert(await runPage.locator('[data-run-select]').first().isChecked());
 assert.equal(await runPage.locator('.run-visibility').first().getAttribute('aria-pressed'),'false');
 assert.deepEqual(await runPage.locator('.runs-table').evaluate(node=>[node.scrollTop,node.scrollLeft]),tableScroll);
 const releaseDialog=await runs.hold(),beforeDialog=await runPage.evaluate(()=>window.replacements);
 await runPage.evaluate(()=>window.poll());await runPage.waitForFunction(()=>document.querySelector('[data-action="refresh"]').disabled);
 await runPage.locator('[data-inspect-run]').first().click();
 const inspecting=await runPage.locator('#detail-dialog').getAttribute('data-run');
 releaseDialog();await runPage.waitForFunction(()=>!document.querySelector('[data-action="refresh"]').disabled);await settle(runPage);
 assert.equal(await runPage.evaluate(()=>window.replacements),beforeDialog);
 assert.equal(await runPage.locator('#detail-dialog[open]').getAttribute('data-run'),inspecting);
 await runPage.locator('#detail-dialog [data-action="close-dialog"]').click();
 await runPage.waitForFunction(before=>window.replacements>before,beforeDialog);
 await runPage.close();
 await _verifyRichCardArrival({open,poll,add,table});
 await _verifyPendingVega({open,vega,settle});
 await _verifyIdleControls({open,poll,settle});
 await _verifyPickerCompletion({open,settle});
 await _verifyPendingPlotly({open,settle});
 await _verifyVegaResize({open,vega});
 await _verifyVegaHeight({open,vega});
 await _verifyLargeChartCapture({open});
 assert.deepEqual(errors,[]);
 console.log(JSON.stringify({scroll:['workspace','single-run','logs','mobile','nested-table-xy','legend','full-runs-table','mobile-return'],state:['disclosures','focus-caret','vega','media-playback','zoom','visibility','selection','sections','pagination'],interactions:['in-flight-menu','in-flight-dialog','pointer','native-select','color-picker','composition','text-selection','chart-inspection'],rendersPerRefresh:1,pageErrors:0}));
}finally{await browser.close();await new Promise(resolve=>server.close(resolve));}

/**
 * Purpose: Preserve an existing card when a newer run adds a preceding card.
 * Input: The browser fixture and the functions that add events and refresh.
 * Output: Assertions for the original table, disclosure, and focused input.
 */
async function _verifyRichCardArrival({open, poll, add, table}) {
    const {page} = await open('view=data');
    try {
        await page.locator('[data-vega-ready]').waitFor({state: 'attached'});
        const card = page.locator('.rich-card').filter({
            has: page.getByRole('button', {
                name: 'Synthetic scroll run 0', exact: true,
            }),
        }).filter({
            has: page.getByRole('heading', {name: 'outputs/table', exact: true}),
        });
        await card.locator('[data-table-search]').fill('Synthetic row');
        const before = await card.evaluate(node => {
            const table = node.querySelector('.logged-table');
            table.querySelector('details').open = true;
            table.scrollTop = 220;
            table.scrollLeft = 180;
            const input = node.querySelector('[data-table-search]');
            input.focus({preventScroll: true});
            input.setSelectionRange(1, 5, 'backward');
            return [table.scrollTop, table.scrollLeft];
        });
        assert(before[0] > 0, 'The existing logged table must scroll vertically');
        add(40, 'run_start', {name: 'Newer synthetic run', tags: ['synthetic-demo']});
        add(40, 'history', {step: 0, metrics: {'outputs/table': table}});
        await poll(page);
        assert.deepEqual(await card.evaluate(node => {
            const table = node.querySelector('.logged-table');
            return [table.scrollTop, table.scrollLeft];
        }), before);
        assert(await card.locator('details').first().evaluate(node => node.open));
        assert(await card.locator('[data-table-search]').evaluate(node =>
            document.activeElement === node && node.selectionStart === 1 &&
            node.selectionEnd === 5 && node.selectionDirection === 'backward'
        ));
        const newCard = page.locator('.rich-card').filter({
            has: page.getByRole('button', {name: 'Newer synthetic run', exact: true}),
        });
        assert.equal(await newCard.locator('details[open]').count(), 0);
    } finally {
        await page.close();
    }
}

/**
 * Purpose: Wait for the current Vega render before the final scroll restore.
 * Input: A browser page with the real Vega runtime and a chart value.
 * Output: Assertions for a rerender that starts before the prior render ends.
 */
async function _verifyPendingVega({open, vega, settle}) {
    const {page} = await open();
    try {
        await page.evaluate(async value => {
            const {hydrateVega} = await import('/vega-view.mjs');
            const {replaceView} = await import('/media-view.mjs');
            const {captureViewState, restoreViewState} =
                await import('/view-state.mjs');
            const root = document.createElement('div');
            root.style.cssText = 'width:300px;height:160px;overflow:auto';
            document.body.append(root);
            const html = '<div data-vega-id="review-probe" ' +
                'data-vega-key="review-probe"></div>';
            root.innerHTML = html;
            const registry = new Map([['review-probe', value]]);
            const original = window.vega.View.prototype.runAsync;
            let release;
            const gate = new Promise(resolve => { release = resolve; });
            window.vega.View.prototype.runAsync = function (...args) {
                window.probeCalls++;
                return gate.then(() => original.apply(this, args));
            };
            window.probeCalls = 0;
            const first = hydrateVega(root, registry);
            window.pendingVegaProbe = {
                root, html, registry, first, release, original,
                hydrateVega, replaceView, captureViewState, restoreViewState,
            };
        }, vega);
        await page.waitForFunction(() => window.probeCalls === 1);
        await page.evaluate(() => {
            const probe = window.pendingVegaProbe;
            const placeholder = document.createElement('div');
            placeholder.style.height = '1000px';
            probe.root.firstElementChild.append(placeholder);
            probe.root.scrollTop = 240;
            const snapshot = probe.captureViewState(probe.root);
            probe.replaceView(probe.root, probe.html);
            const restoreScroll = probe.restoreViewState(probe.root, snapshot);
            window.latestProbeFinished = false;
            probe.latest = probe.hydrateVega(probe.root, probe.registry).then(() => {
                restoreScroll();
                window.latestProbeFinished = true;
            });
        });
        await settle(page);
        assert.equal(await page.evaluate(() => window.latestProbeFinished), false,
            'The latest render must not finish before its pending chart');
        const result = await page.evaluate(async () => {
            const probe = window.pendingVegaProbe;
            try {
                probe.release();
                await Promise.all([probe.first, probe.latest]);
                return {
                    ready: probe.root.firstElementChild.dataset.vegaReady,
                    top: probe.root.scrollTop,
                };
            } finally {
                window.vega.View.prototype.runAsync = probe.original;
                probe.root.remove();
            }
        });
        assert.equal(result.ready, 'true');
        assert.equal(result.top, 240);
    } finally {
        await page.close();
    }
}

/**
 * Purpose: Apply live data after a control interaction without a required blur.
 * Input: The browser fixture and the function that invokes the real refresh.
 * Output: Assertions for idle focus and an actual open native select picker.
 */
async function _verifyIdleControls({open, poll, settle}) {
    const {page} = await open();
    try {
        for (const selector of [
            '[data-run-page-size]', '.run-color',
            '[data-vega-ready] input[type="range"]',
        ]) {
            const control = page.locator(selector).first();
            await control.focus();
            await poll(page);
            assert(await control.evaluate(node => document.activeElement === node));
            await poll(page);
            assert(await control.evaluate(node => document.activeElement === node));
        }
        const select = page.locator('[data-run-page-size]');
        await _observeNativePickerRenders(page);
        await select.click();
        assert(await select.evaluate(node => node.matches(':open')));
        const before = await page.evaluate(() => window.replacements);
        await page.evaluate(() => window.poll());
        await page.waitForFunction(() =>
            !document.querySelector('[data-action="refresh"]').disabled
        );
        await settle(page);
        const picker = await page.evaluate(() => ({
            open: document.querySelector('[data-run-page-size]').matches(':open'),
            renders: window.pickerRenderStates,
            replacements: window.replacements,
        }));
        // Native pickers can close as browser automation advances. Check the
        // picker at the DOM mutation boundary, before a render can close it.
        assert(picker.renders.every(isOpen => !isOpen),
            'A refresh must not replace the view while a native picker is open');
        if (picker.open) assert.equal(picker.replacements, before);
        const afterPoll = picker.replacements;
        await select.selectOption('50');
        await page.waitForFunction(before => window.replacements > before, afterPoll);
        assert(await select.evaluate(node => document.activeElement === node));
        await poll(page);
    } finally {
        await page.close();
    }
}

/**
 * Purpose: Observe native picker state before the app replaces its root contents.
 * Input: A browser page with the real dashboard root.
 * Output: Picker states at both supported DOM replacement entry points.
 */
async function _observeNativePickerRenders(page) {
    await page.evaluate(() => {
        const root = document.querySelector('#app');
        const property = Object.getOwnPropertyDescriptor(Element.prototype, 'innerHTML');
        const replaceChildren = root.replaceChildren;
        window.pickerRenderStates = [];
        const observe = () => window.pickerRenderStates.push(
            Boolean(root.querySelector('select:open,input:open'))
        );
        root.replaceChildren = function (...nodes) {
            observe();
            return replaceChildren.apply(this, nodes);
        };
        Object.defineProperty(root, 'innerHTML', {
            get() { return property.get.call(this); },
            set(value) {
                observe();
                property.set.call(this, value);
            },
        });
    });
}

/**
 * Purpose: End native picker pointer ownership before a refresh is requested.
 * Input: A browser page with real native controls and the refresh renderer.
 * Output: Assertions for a select choice and a color picker cancel event.
 */
async function _verifyPickerCompletion({open, settle}) {
    const {page} = await open();
    try {
        await page.evaluate(async () => {
            const {createRefreshRenderer} = await import('/view-state.mjs');
            const root = document.createElement('div');
            root.id = 'picker-probe';
            root.innerHTML = '<select><option>A</option><option>B</option>' +
                '</select><input type="color">';
            document.body.append(root);
            window.pickerProbe = {
                root, renders: 0,
                renderer: createRefreshRenderer(root, () => {
                    window.pickerProbe.renders++;
                }),
            };
        });
        const select = page.locator('#picker-probe select');
        await select.click();
        assert(await select.evaluate(node => node.matches(':open')));
        await select.selectOption('B');
        // Headless Chromium keeps its popup open after selectOption.
        // End that popup without a document pointerup or a window blur.
        await select.evaluate(node => node.blur());
        assert.equal(await select.evaluate(node => node.matches(':open')), false);
        await page.evaluate(() => window.pickerProbe.renderer.request());
        await settle(page);
        assert.equal(await page.evaluate(() => window.pickerProbe.renders), 1,
            'A completed picker must not block the next refresh');
        await page.evaluate(() => {
            const probe = window.pickerProbe;
            const select = probe.root.querySelector('select');
            select.dispatchEvent(new PointerEvent('pointerdown', {bubbles: true}));
            select.dispatchEvent(new Event('change', {bubbles: true}));
            probe.renderer.request();
        });
        await settle(page);
        assert.equal(await page.evaluate(() => window.pickerProbe.renders), 2,
            'A changed picker must clear a consumed pointerup');
        await page.evaluate(() => {
            const probe = window.pickerProbe;
            const color = probe.root.querySelector('input');
            color.dispatchEvent(new PointerEvent('pointerdown', {bubbles: true}));
            color.dispatchEvent(new Event('cancel', {bubbles: true}));
            probe.renderer.request();
        });
        await settle(page);
        assert.equal(await page.evaluate(() => window.pickerProbe.renders), 3,
            'A cancelled picker must clear a consumed pointerup');
    } finally {
        await page.close();
    }
}

/**
 * Purpose: Wait for a replacement Plotly mount before the final scroll restore.
 * Input: A browser page with a delayed request for the real Plotly library.
 * Output: Assertions for concurrent hydration, readiness, and saved scroll.
 */
async function _verifyPendingPlotly({open, settle}) {
    const {page} = await open();
    let release;
    const gate = new Promise(resolve => { release = resolve; });
    await page.route('**/plotly-strict-4.1.1.min.js', async route => {
        await gate;
        await route.continue();
    });
    try {
        await page.evaluate(async () => {
            const {hydratePlotly} = await import('/plotly-view.mjs');
            const {replaceView} = await import('/media-view.mjs');
            const {captureViewState, restoreViewState} =
                await import('/view-state.mjs');
            const root = document.createElement('div');
            root.style.cssText = 'width:300px;height:160px;overflow:auto';
            document.body.append(root);
            const html = '<div data-plotly-id="pending-plotly" ' +
                'data-plotly-key="pending-plotly"></div>';
            root.innerHTML = html;
            const registry = new Map([['pending-plotly', {
                id: 'pending-plotly',
                spec: {
                    data: [{type: 'scatter', x: [0, 1], y: [1, 2]}],
                    layout: {height: 700},
                },
            }]]);
            const first = hydratePlotly(root, registry);
            window.pendingPlotlyProbe = {
                root, html, registry, first, hydratePlotly, replaceView,
                captureViewState, restoreViewState,
            };
        });
        await page.waitForFunction(() => document.querySelector(
            'script[src*="plotly-strict-4.1.1.min.js"]'
        ));
        await page.evaluate(() => {
            const probe = window.pendingPlotlyProbe;
            probe.root.firstElementChild.style.height = '1000px';
            probe.root.scrollTop = 240;
            const snapshot = probe.captureViewState(probe.root);
            probe.replaceView(probe.root, probe.html);
            const restoreScroll = probe.restoreViewState(probe.root, snapshot);
            window.latestPlotlyFinished = false;
            probe.latest = Promise.resolve(probe.hydratePlotly(
                probe.root, probe.registry
            )).then(() => {
                restoreScroll();
                window.latestPlotlyFinished = true;
            });
            window.secondPlotlyFinished = false;
            probe.second = Promise.resolve(probe.hydratePlotly(
                probe.root, probe.registry
            )).then(() => { window.secondPlotlyFinished = true; });
        });
        await settle(page);
        assert.equal(await page.evaluate(() => window.latestPlotlyFinished), false,
            'The latest render must wait for the replacement Plotly mount');
        assert.equal(await page.evaluate(() => window.secondPlotlyFinished), false,
            'Another caller must await the same pending mount');
        release();
        const result = await page.evaluate(async () => {
            const probe = window.pendingPlotlyProbe;
            await Promise.all([probe.first, probe.latest, probe.second]);
            return {
                ready: probe.root.firstElementChild.dataset.plotlyReady,
                top: probe.root.scrollTop,
            };
        });
        assert.equal(result.ready, 'true');
        assert.equal(result.top, 240);
    } finally {
        release();
        await page.close();
    }
}

/**
 * Purpose: Resize a retained container-sized Vega chart after a layout change.
 * Input: A browser page and a chart with a bound inspection control.
 * Output: Assertions for chart dimensions, node identity, and control state.
 */
async function _verifyVegaResize({open, vega}) {
    const {page} = await open();
    try {
        const result = await page.evaluate(async value => {
            const {hydrateVega} = await import('/vega-view.mjs');
            const {replaceView} = await import('/media-view.mjs');
            const root = document.createElement('div');
            root.style.width = '300px';
            document.body.append(root);
            const html = '<div data-vega-id="responsive-vega" ' +
                'data-vega-key="responsive-vega"></div>';
            const registry = new Map([['responsive-vega', {
                table: value.table,
                spec: {
                    width: 'container', height: 180, mark: 'point',
                    params: [{name: 'inspection', value: 0,
                        bind: {input: 'range', min: 0, max: 10}}],
                    encoding: {
                        x: {field: 'x', type: 'quantitative'},
                        y: {field: 'y', type: 'quantitative'},
                    },
                },
            }]]);
            root.innerHTML = html;
            await hydrateVega(root, registry);
            const node = root.firstElementChild;
            const input = node.querySelector('input');
            input.value = '7';
            input.dispatchEvent(new Event('input', {bubbles: true}));
            const before = node.querySelector('svg').getBoundingClientRect().width;
            root.style.width = '600px';
            replaceView(root, html);
            await hydrateVega(root, registry);
            const after = node.querySelector('svg').getBoundingClientRect().width;
            const same = root.firstElementChild === node;
            const control = root.querySelector('input').value;
            root.style.width = '240px';
            replaceView(root, html);
            await hydrateVega(root, registry);
            const narrow = node.querySelector('svg').getBoundingClientRect().width;
            root.remove();
            return {before, after, narrow, same, control};
        }, vega);
        assert(result.after > result.before + 200,
            `A wider panel must resize its retained chart: ${JSON.stringify(result)}`);
        assert(result.narrow < result.before,
            'A narrower panel must resize its retained chart');
        assert.equal(result.same, true);
        assert.equal(result.control, '7');
    } finally {
        await page.close();
    }
}

/**
 * Purpose: Keep an auto-height Vega host stable across repeated refreshes.
 * Input: A browser page and the table for a container-height chart.
 * Output: Assertions for stable dimensions, node identity, and a bound control.
 */
async function _verifyVegaHeight({open, vega}) {
    const {page} = await open();
    try {
        const result = await page.evaluate(async value => {
            const {hydrateVega} = await import('/vega-view.mjs');
            const {replaceView} = await import('/media-view.mjs');
            const root = document.createElement('div');
            root.style.width = '400px';
            document.body.append(root);
            const html = '<div class="plotly-container" ' +
                'data-vega-id="auto-height-vega" ' +
                'data-vega-key="auto-height-vega"></div>';
            const registry = new Map([['auto-height-vega', {
                table: value.table,
                spec: {
                    width: 300, height: 'container', mark: 'point',
                    params: [{name: 'inspection', value: 0,
                        bind: {input: 'range', min: 0, max: 10}}],
                    encoding: {
                        x: {field: 'x', type: 'quantitative'},
                        y: {field: 'y', type: 'quantitative'},
                    },
                },
            }]]);
            root.innerHTML = html;
            await hydrateVega(root, registry);
            const node = root.firstElementChild;
            const input = node.querySelector('input');
            input.value = '7';
            input.dispatchEvent(new Event('input', {bubbles: true}));
            const heights = [node.clientHeight];
            for (let refresh = 0; refresh < 5; refresh++) {
                replaceView(root, html);
                await hydrateVega(root, registry);
                heights.push(node.clientHeight);
            }
            const same = root.firstElementChild === node;
            const control = root.querySelector('input').value;
            root.remove();
            return {heights, same, control};
        }, vega);
        assert(result.heights[0] > 0, 'The chart must have a rendered height');
        assert(result.heights.every(height => height === result.heights[0]),
            `An auto-height chart must not grow: ${JSON.stringify(result)}`);
        assert.equal(result.same, true);
        assert.equal(result.control, '7');
    } finally {
        await page.close();
    }
}

/**
 * Purpose: Capture HTML state without a scroll read for each chart mark.
 * Input: A browser page with a large SVG and an arbitrary HTML scroll pane.
 * Output: Assertions for bounded SVG reads and restored nested HTML state.
 */
async function _verifyLargeChartCapture({open}) {
    const {page} = await open();
    try {
        const result = await page.evaluate(async () => {
            const {captureViewState, restoreViewState} =
                await import('/view-state.mjs');
            const root = document.createElement('div');
            document.body.append(root);
            const html = '<div data-view-key="large-chart-scroll" ' +
                'style="width:280px;height:160px;overflow:auto">' +
                '<svg width="800" height="1000">' +
                '<circle cx="20" cy="20" r="2"/>'.repeat(30000) +
                '</svg></div><details><summary>Details</summary>' +
                '<input value="inspection"></details>';
            root.innerHTML = html;
            const pane = root.firstElementChild;
            pane.scrollTop = 240;
            pane.scrollLeft = 80;
            root.querySelector('details').open = true;
            const input = root.querySelector('input');
            input.focus({preventScroll: true});
            input.setSelectionRange(1, 5, 'backward');
            let svgScrollReads = 0;
            const originals = new Map();
            for (const property of ['scrollTop', 'scrollLeft']) {
                originals.set(property, Object.getOwnPropertyDescriptor(
                    SVGElement.prototype, property
                ));
                Object.defineProperty(SVGElement.prototype, property, {
                    configurable: true,
                    get() { svgScrollReads++; return 0; },
                });
            }
            try {
                const before = [pane.scrollTop, pane.scrollLeft];
                const snapshot = captureViewState(root);
                root.innerHTML = html;
                restoreViewState(root, snapshot);
                return {
                    svgScrollReads, before,
                    after: [root.firstElementChild.scrollTop,
                        root.firstElementChild.scrollLeft],
                    open: root.querySelector('details').open,
                    focus: document.activeElement === root.querySelector('input'),
                    caret: [document.activeElement.selectionStart,
                        document.activeElement.selectionEnd],
                };
            } finally {
                for (const [property, descriptor] of originals) {
                    if (descriptor) {
                        Object.defineProperty(SVGElement.prototype,
                            property, descriptor);
                    } else {
                        delete SVGElement.prototype[property];
                    }
                }
                root.remove();
            }
        });
        assert.equal(result.svgScrollReads, 0,
            'State capture must not read scroll properties for SVG marks');
        assert(result.before[0] > 0 && result.before[1] > 0);
        assert.deepEqual(result.after, result.before);
        assert.equal(result.open, true);
        assert.equal(result.focus, true);
        assert.deepEqual(result.caret, [1, 5]);
    } finally {
        await page.close();
    }
}
