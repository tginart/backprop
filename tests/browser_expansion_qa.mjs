/** Real Vega/Plotly rendering under the deployed CSP using an isolated browser.
 * Architecture: synthetic logged charts -> genuine local libraries -> inspect
 * rendered geometry, denied URLs, and desktop/mobile layout. No user profile. */
import assert from 'node:assert/strict';
import {createServer} from 'node:http';
import {readFile,mkdir} from 'node:fs/promises';
import {join,extname} from 'node:path';
import {fileURLToPath} from 'node:url';
import {spawnSync} from 'node:child_process';
const root=fileURLToPath(new URL('../',import.meta.url));
const {chromium}=await import(process.env.PLAYWRIGHT_MODULE || 'playwright');
const html='<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/styles.css"></head><body><main style="padding:24px"><h1>Backprop: expanded chart support</h1><section data-vega-id="curve" class="plotly-container"></section><section data-vega-id="residual" class="plotly-container"></section><section data-vega-id="denied"></section><section data-plotly-id="three" data-plotly-key="three" class="plotly-container"></section></main><script type="module" src="/fixture.mjs"></script></body></html>';
const policy=spawnSync(process.env.PYTHON || 'python3',['-c','import sys;sys.path.insert(0,sys.argv[1]);from deploy import apply_content_security_policy;sys.stdout.buffer.write(apply_content_security_policy(sys.stdin.buffer.read(),{"host":"https://foundry-test.invalid"}))',join(root,'deploy')],{input:html,encoding:'utf8'});assert.equal(policy.status,0);
const preset=spawnSync(process.env.BACKPROP_PYTHON || 'python3',['-c',"import json;from backprop.legacy_charts import enrich_preset;print(json.dumps(enrich_preset('wandb/residuals_plot/v1',{'data':{'name':'wandb'},'mark':'point','encoding':{'x':{'field':'y_pred','type':'quantitative'},'y':{'field':'residuals','type':'quantitative'}}})))"],{cwd:root,env:{...process.env,PYTHONPATH:join(root,'sdk')},encoding:'utf8'});assert.equal(preset.status,0,preset.stderr);
const fixture=`import {hydrateVega} from '/vega-view.mjs';import {hydratePlotly} from '/plotly-view.mjs';
const spec={data:{name:'wandb'},mark:'line',encoding:{x:{field:'epoch',type:'quantitative'},y:{field:'loss',type:'quantitative'}},width:280,height:180};
const table={columns:['epoch','loss'],data:[[0,4],[1,2],[2,1]]};
await hydrateVega(document,new Map([['curve',{spec,table}],['residual',{spec:${preset.stdout.trim()},table:{columns:['y_pred','residuals','dataset'],data:[[1,.2,'train'],[2,-.5,'train'],[3,.1,'test']]}}],['denied',{spec:{...spec,data:{url:'https://invalid.example/private'}},table}]]));
await hydratePlotly(document,new Map([['three',{id:'three',spec:{data:[{type:'scatter3d',x:[0,1,2],y:[1,3,2],z:[0,2,4],mode:'lines+markers'}],layout:{title:{text:'3D training trajectory'},height:350}}}]]),{getMediaUrl:async()=>{throw new Error('Unexpected media')}});
window.ready=true;`;
const server=createServer(async(req,res)=>{try{let path=new URL(req.url,'http://localhost').pathname;let body=path==='/'?policy.stdout:path==='/fixture.mjs'?fixture:await readFile(join(root,'web',path));res.setHeader('Content-Type',path==='/'?'text/html':path.endsWith('.js')||path.endsWith('.mjs')?'text/javascript':path.endsWith('.css')?'text/css':'application/json');res.end(body);}catch{res.writeHead(404);res.end();}});
await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
const url=`http://127.0.0.1:${server.address().port}`;
const browser=await chromium.launch({executablePath:process.env.CHROMIUM_EXECUTABLE || process.env.BROWSER_EXECUTABLE,headless:true,args:['--enable-unsafe-swiftshader']});
try{
 const page=await browser.newPage({viewport:{width:1100,height:850}}),errors=[],external=[];
 page.on('pageerror',error=>errors.push(error.message));page.on('request',req=>{if(/^https?:/.test(req.url())&&!req.url().startsWith(url))external.push(req.url());});
 await page.goto(url);await page.waitForFunction(()=>window.ready,null,{timeout:60000}).catch(async error=>{throw new Error(JSON.stringify({errors,body:await page.locator("body").innerText(),error:error.message}));});
 const curve=page.locator('[data-vega-id="curve"]');
 assert.equal(await curve.getAttribute('data-vega-ready'),'true',await curve.innerText());
 assert(await curve.locator('svg path').count()>0);
 const residual=page.locator('[data-vega-id="residual"]');
 assert.equal(await residual.getAttribute('data-vega-ready'),'true',await residual.innerText());
 assert(await residual.locator('svg path').count()>0);
 assert((await residual.locator('svg').textContent()).includes('−0.5'), 'Negative tick labels retain UTF-8 encoding');
 assert((await page.locator('[data-vega-id="denied"]').innerText()).includes('cannot load external'));
 await page.locator('[data-plotly-id="three"][data-plotly-ready]').waitFor({timeout:60000});
 assert.equal(await page.locator('[data-plotly-id="three"] .plotly-graph').evaluate(node=>node._fullData[0].type),'scatter3d');
 await mkdir(join(root,'test-results'),{recursive:true});await page.screenshot({path:join(root,'test-results/expansion-desktop.png'),fullPage:true});
 await page.setViewportSize({width:390,height:844});await page.waitForTimeout(200);assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));await page.screenshot({path:join(root,'test-results/expansion-mobile.png'),fullPage:true});
 assert.deepEqual(errors,[]);assert.deepEqual(external,[]);console.log(JSON.stringify({vega:true,plotly3d:true,mobile:true,externalRequests:0}));
}finally{await browser.close();await new Promise(resolve=>server.close(resolve));}
