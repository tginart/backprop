/**
 * Architecture: saved SDK layout figures -> release CSP -> native/viewer queue
 * and retained updates -> raw final geometry and actual JavaScript failures.
 * Main caller: explicit focused investigation, never Node test discovery.
 * Why this exists: observe a real update race before changing its lifecycle.
 */
import {spawnSync} from 'node:child_process';
import {createServer} from 'node:http';
import {readFile,writeFile} from 'node:fs/promises';
import {join,extname} from 'node:path';
import {fileURLToPath} from 'node:url';
import assert from 'node:assert/strict';
const root=fileURLToPath(new URL('../',import.meta.url)),python=process.env.BACKPROP_PYTHON||'python3';
const encoded=spawnSync(python,['-B',join(root,'tests/plotly_layout_fixtures.py')],{encoding:'utf8',cwd:root,env:{...process.env,PYTHONPATH:join(root,'sdk')}});assert.equal(encoded.status,0,encoded.stderr);const fixtures=JSON.parse(encoded.stdout);
const html='<!doctype html><html><head><link rel="stylesheet" href="/styles.css"></head><body><section id="viewer"></section><script type="module" src="/fixture.mjs"></script></body></html>';
const policy=spawnSync(python,['-B','-c','import sys;sys.path.insert(0,sys.argv[1]);from deploy import apply_content_security_policy;sys.stdout.buffer.write(apply_content_security_policy(sys.stdin.buffer.read(),{"host":"https://foundry-test.invalid"}))',join(root,'deploy')],{input:html,encoding:'utf8'});assert.equal(policy.status,0,policy.stderr);
const script=`import {hydratePlotly} from '/plotly-view.mjs';import {replaceView} from '/media-view.mjs';window.values=${JSON.stringify(fixtures)};window.registry=new Map();window.exceptions=[];for(const type of ['error','unhandledrejection'])window.addEventListener(type,event=>{const error=type==='error'?event.error:event.reason;window.exceptions.push({source:type,name:error?.name,message:error?.message,stack:error?.stack});});window.show=name=>{window.registry.set('same',{id:'same',...window.values.values[name]});replaceView(document.querySelector('#viewer'),'<div data-plotly-id="same" data-plotly-key="same"></div>');hydratePlotly(document.querySelector('#viewer'),window.registry);};window.update=value=>{window.registry.set('same',{id:'same',...value});hydratePlotly(document.querySelector('#viewer'),window.registry);};window.ready=true;`;
const server=createServer(async(req,res)=>{try{const path=new URL(req.url,'http://localhost').pathname;assert(!path.includes('..'));res.setHeader('Content-Type',path==='/'?'text/html':extname(path)==='.css'?'text/css':'text/javascript');res.end(path==='/'?policy.stdout:path==='/fixture.mjs'?script:await readFile(join(root,'web',path)));}catch{res.statusCode=404;res.end();}});await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
const origin='http://127.0.0.1:'+server.address().port,playwright=await import(process.env.PLAYWRIGHT_MODULE||'playwright');
const browser=await playwright.chromium.launch({headless:true,executablePath:'/Applications/Brave Browser.app/Contents/MacOS/Brave Browser'}),output={browser:await browser.version(),cases:[]};
try{
 for(const name of ['ranges_styles','plain_working']){
  const page=await browser.newPage(),transport=[];page.on('pageerror',error=>transport.push(error.message));await page.goto(origin);await page.waitForFunction(()=>window.ready);await page.evaluate(name=>window.show(name),name);await page.waitForFunction(()=>document.querySelector('.plotly-graph')?._transitionData?._frameHash?.next);
  await page.evaluate(()=>{const node=document.querySelector('.plotly-graph');window.events=[];for(const event of ['plotly_animatingframe','plotly_transitioning','plotly_transitioned','plotly_animated','plotly_afterplot'])node.on(event,value=>window.events.push({event,name:value?.name,time:performance.now()}));});
  await page.locator('g.updatemenu-button').filter({hasText:/^Sequence$/}).click();await page.waitForFunction(()=>document.querySelector('.plotly-graph')._transitionData._frameQueue.length>0);
  const observed=await page.evaluate(async()=>{const node=document.querySelector('.plotly-graph'),before={y:Array.from(node._fullData[0].y),range:node._fullLayout.yaxis.range,queued:node._transitionData._frameQueue.length};window.update(window.values.retained[1]);window.update(window.values.retained[0]);window.update(window.values.retained[1]);await new Promise(resolve=>setTimeout(resolve,1400));return{before,after:{y:Array.from(node._fullData[0].y),range:node._fullLayout.yaxis.range,current:node._fullLayout._currentFrame,frames:Object.fromEntries(Object.entries(node._transitionData._frameHash).map(([key,value])=>[key,value.data?.[0]?.y])),queue:node._transitionData._frameQueue.length},expected_y:window.values.retained[1].spec.data[0].y,events:window.events,exceptions:window.exceptions,alerts:[...document.querySelectorAll('[role="alert"]')].map(x=>x.innerText)};});output.cases.push({name,...observed,transport});await page.close();
 }
 // Invalid input deliberately probes retry bookkeeping, independent of saved
 // Figure encoding. The same rejected descriptor should attempt validation.
 const page=await browser.newPage();await page.goto(origin);await page.waitForFunction(()=>window.ready);await page.evaluate(()=>window.show('plain_working'));await page.waitForFunction(()=>document.querySelector('.plotly-graph')?._transitionData?._frameHash?.next);
 const retry=await page.evaluate(async()=>{const value=structuredClone(window.values.values.plain_working);value.spec.data[0].type='not-a-real-trace';window.update(value);await new Promise(resolve=>setTimeout(resolve,20));const alert=document.querySelector('[role="alert"]');const first=alert?.innerText;alert?.remove();window.update(value);await new Promise(resolve=>setTimeout(resolve,20));return{first,second:document.querySelector('[role="alert"]')?.innerText||null};});output.retry=retry;await page.close();
 await writeFile(process.env.PLOTLY_CONCURRENT_PROBE_OUTPUT||'/tmp/backprop-batch52-plotly-concurrent-probe.json',JSON.stringify(output,null,2)+'\n');console.log(JSON.stringify(output));
}finally{await browser.close();await new Promise(resolve=>server.close(resolve));}
