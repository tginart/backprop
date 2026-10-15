/** Real application -> synthetic Foundry transport -> live metric controls.
 * Checks slider capture/focus, raw overlays, panel isolation, zoom, expanded
 * copies, reload persistence and responsive layout under the release CSP.
 * Main caller: node tests/browser_chart_smoothing_qa.mjs with Playwright installed.
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
const config={host,client_id:'synthetic-client',stream_dataset_rid:'ri.foundry.main.dataset.smoothing-events',media_dataset_rid:'ri.foundry.main.dataset.smoothing-media'};
const events=[];
const add=(type,payload)=>events.push({schema_version:1,event_id:`smoothing-${events.length}`,run_id:'synthetic-run',project:'smoothing-demo',entity:'demo-user',type,timestamp:new Date(Date.UTC(2026,9,7,12,0,events.length)).toISOString(),payload});
add('run_start',{name:'Synthetic smoothing demo',config:{seed:1},tags:['synthetic-demo']});
for(let step=0;step<=100;step++)add('history',{step,metrics:{'train/loss':2+Math.exp(-step/30)+(step%2?.6:-.6),'train/accuracy':.5+step/250}});
add('run_finish',{state:'finished'});
const original=JSON.stringify(events), records=workflowFoundryRecords(events);
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
let browser;
try{
  browser=await chromium.launch({headless:true,executablePath:process.env.CHROMIUM_EXECUTABLE});
  const page=await browser.newPage({viewport:{width:1440,height:900}}),errors=[];
  page.on('pageerror',error=>errors.push(error.message));
  await page.addInitScript(()=>sessionStorage.setItem('backprop.oauth.session',JSON.stringify({accessToken:'synthetic-test-only',expiresAt:Date.now()+3600000})));
  await page.route(`${host}/**`,async route=>{
    const url=new URL(route.request().url());
    if(url.pathname.endsWith('/execute'))return route.fulfill({json:{queryId:'smoothing-fixture',type:'succeeded'}});
    if(url.pathname.endsWith('/getResults'))return route.fulfill({body:workflowFoundryCsv(records),contentType:'text/csv'});
    if(url.pathname.endsWith('/getEndOffsets'))return route.fulfill({json:{'0':String(events.length)}});
    if(url.pathname.endsWith('/getRecords')){
      const start=Number(url.searchParams.get('startOffset')||0),limit=Number(url.searchParams.get('limit')||100);
      return route.fulfill({json:records.slice(start,start+limit).map((record,i)=>({offset:String(start+i),value:record}))});
    }
    return route.fulfill({status:404});
  });
  await page.goto(`http://127.0.0.1:${server.address().port}/?project=smoothing-demo`);
  const card=page.locator('#app [data-metric="train/loss"]'), other=page.locator('#app [data-metric="train/accuracy"]');
  const slider=card.locator('[data-panel-smoothing]'), line=card.locator('.chart-line');
  await slider.waitFor();
  const before=await line.getAttribute('d'), otherBefore=await other.locator('.chart-line').getAttribute('d');
  assert.equal(await card.locator('.chart-raw-line').count(),0);
  await slider.focus();
  await slider.evaluate(node=>{window.originalSlider=node;node.value='.75';node.dispatchEvent(new Event('input',{bubbles:true}));});
  assert.notEqual(await line.getAttribute('d'),before,'input redraws before change/release');
  assert(await slider.evaluate(node=>node===window.originalSlider && node===document.activeElement),'slider keeps focus and DOM identity');
  assert.equal(await card.locator('output').textContent(),'0.75');
  assert.equal(await card.locator('.chart-raw-line').count(),1);
  assert.equal(await other.locator('.chart-line').getAttribute('d'),otherBefore,'another metric is unaffected');
  await slider.dispatchEvent('change');
  const saved=await page.evaluate(()=>JSON.parse(localStorage.getItem('backprop.exploration.v1')));
  assert.equal(new Map(saved.panelSettings).get(JSON.stringify(['demo-user/smoothing-demo','train/loss'])).smoothing,.75);

  // Native dragging updates mid-gesture without replacing the captured control.
  const bounds=await slider.boundingBox(), x=fraction=>bounds.x+7+fraction*(bounds.width-14), y=bounds.y+bounds.height/2;
  await page.mouse.move(x(.75/.98),y);await page.mouse.down();await page.mouse.move(x(.35/.98),y,{steps:5});
  assert.notEqual(await card.locator('output').textContent(),'0.75');
  assert(await slider.evaluate(node=>node===window.originalSlider));
  await page.mouse.up();

  // Zoom still uses the unsnapped pointer coordinates and survives smoothing.
  const position=async fraction=>card.locator('.chart-svg').evaluate((svg,fraction)=>{
    const rect=svg.querySelector('clipPath rect'),point=new DOMPoint(Number(rect.getAttribute('x'))+Number(rect.getAttribute('width'))*fraction,100).matrixTransform(svg.getScreenCTM());
    return {x:point.x,y:point.y};
  },fraction);
  const start=await position(.2),end=await position(.8);
  await page.mouse.move(start.x,start.y);await page.mouse.down();await page.mouse.move(end.x,end.y);await page.mouse.up();
  assert.equal(await card.locator('[data-reset-chart]').count(),1);
  await slider.evaluate(node=>{node.value='.85';node.dispatchEvent(new Event('input',{bubbles:true}));node.dispatchEvent(new Event('change',{bubbles:true}));});
  assert.equal(await card.locator('[data-reset-chart]').count(),1);

  await card.locator('[data-expand]').click();
  const expanded=page.locator('#chart-dialog [data-metric="train/loss"]');
  assert.equal(await expanded.locator('[data-panel-smoothing]').inputValue(),'0.85');
  await expanded.locator('[data-panel-smoothing]').evaluate(node=>{node.value='.65';node.dispatchEvent(new Event('input',{bubbles:true}));node.dispatchEvent(new Event('change',{bubbles:true}));});
  assert.equal(await slider.inputValue(),'0.65','compact and expanded copies agree');
  await page.getByRole('button',{name:'Close expanded chart',exact:true}).click();
  await page.reload();await slider.waitFor();
  assert.equal(await slider.inputValue(),'0.65');assert.equal(await card.locator('.chart-raw-line').count(),1);
  await slider.focus();await page.keyboard.press('ArrowRight');assert.equal(await slider.inputValue(),'0.66');

  await page.setViewportSize({width:390,height:844});
  const mobile=await slider.boundingBox(),panel=await card.boundingBox();
  assert(mobile.x>=panel.x && mobile.x+mobile.width<=panel.x+panel.width,'mobile slider fits its panel');
  assert(await card.locator('.chart-plot').evaluate(node=>node.clientHeight>=95),'mobile chart keeps room for data');
  await page.screenshot({path:'/tmp/backprop-smoothing-mobile.png',fullPage:true});
  await page.setViewportSize({width:1440,height:900});
  await page.screenshot({path:'/tmp/backprop-smoothing-desktop.png',fullPage:true});
  await slider.evaluate(node=>{node.value='0';node.dispatchEvent(new Event('input',{bubbles:true}));node.dispatchEvent(new Event('change',{bubbles:true}));});
  assert.equal(await card.locator('.chart-raw-line').count(),0);assert.equal(await line.getAttribute('d'),before);
  assert.equal(JSON.stringify(events),original);assert.deepEqual(errors,[]);
  console.log('PASS: live slider, pointer capture, keyboard, faint raw data, isolation, zoom, expansion, persistence and mobile layout; no browser errors');
}finally{
  await browser?.close();await new Promise(resolve=>server.close(resolve));
}
