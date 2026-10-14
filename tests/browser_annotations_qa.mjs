/** Render SDK-produced annotation bytes using the actual Foundry image viewer.
 * Architecture: SDK fixture -> existing deployed CSP -> authenticated-blob style
 * resolver -> box/mask controls and pixels. Uses installed Brave/Playwright only.
 */
import assert from 'node:assert/strict';
import {createServer} from 'node:http';
import {readFile,mkdir,writeFile} from 'node:fs/promises';
import {join} from 'node:path';
import {fileURLToPath} from 'node:url';
import {spawnSync} from 'node:child_process';
const root=fileURLToPath(new URL('../',import.meta.url));
const {chromium}=await import(process.env.PLAYWRIGHT_MODULE || 'playwright');
const python=process.env.BACKPROP_PYTHON || 'python3';
const sample=spawnSync(python,['-B','tests/annotation_replay_fixture.py'],{cwd:root,encoding:'utf8',env:{...process.env,PYTHONPATH:join(root,'sdk')}});assert.equal(sample.status,0,sample.stderr);
const html='<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/styles.css"><link rel="stylesheet" href="/workspace.css"></head><body><main style="padding:24px;max-width:900px"><h1>SDK annotation verification</h1><section id="viewer"></section></main><script type="module" src="/fixture.mjs"></script></body></html>';
const policy=spawnSync(python,['-c','import sys;sys.path.insert(0,sys.argv[1]);from deploy import apply_content_security_policy;sys.stdout.buffer.write(apply_content_security_policy(sys.stdin.buffer.read(),{"host":"https://foundry-test.invalid"}))',join(root,'deploy')],{input:html,encoding:'utf8'});assert.equal(policy.status,0,policy.stderr);
const fixture=`import {imageViewer} from '/media-view.mjs';
const data=await (await fetch('/fixture.json')).json();
const resolve=async ref=>{const bytes=Uint8Array.from(atob(data.files[ref.path]),c=>c.charCodeAt(0));return URL.createObjectURL(new Blob([bytes],{type:ref.mime_type||'image/png'}));};
document.querySelector('#viewer').append(await imageViewer(data.image,await resolve(data.image),resolve));window.ready=true;`;
const server=createServer(async(req,res)=>{try{const path=new URL(req.url,'http://localhost').pathname;const body=path==='/'?policy.stdout:path==='/fixture.mjs'?fixture:path==='/fixture.json'?sample.stdout:await readFile(join(root,'web',path));res.setHeader('Content-Type',path==='/'?'text/html':path.endsWith('.mjs')?'text/javascript':path.endsWith('.css')?'text/css':'application/json');res.end(body);}catch{res.writeHead(404);res.end();}});
await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));const url=`http://127.0.0.1:${server.address().port}`;
const browser=await chromium.launch({executablePath:process.env.BROWSER_EXECUTABLE,headless:true});
try{
 const page=await browser.newPage({viewport:{width:1050,height:700}}),errors=[],external=[];
 page.on('pageerror',error=>errors.push(error.message));page.on('request',req=>{if(/^https?:/.test(req.url())&&!req.url().startsWith(url))external.push(req.url());});
 await page.addInitScript(()=>{CanvasRenderingContext2D.prototype.getImageData=()=>{throw new Error('Mask class IDs must never use canvas readback');};});
 await page.goto(url);await page.waitForFunction(()=>window.ready);await page.locator('canvas.annotation-overlay').waitFor();
 assert.equal(await page.locator('.annotation-overlay rect').count(),1);
 const rect=page.locator('.annotation-overlay rect');for(const [key,value] of Object.entries({x:'16',y:'8',width:'32',height:'16'}))assert.equal(await rect.getAttribute(key),value);
 assert((await page.locator('#viewer').innerText()).includes('Foreground 255'));assert((await page.locator('#viewer').innerText()).includes('Target box'));
 assert.deepEqual(await page.locator('canvas.annotation-overlay').evaluate(n=>[n.width,n.height]),[64,32]);
 assert.deepEqual(await page.locator('.annotation-legend').last().locator('span').allTextContents(),['Background','Foreground 255']);
 const bounds=await page.locator('.annotation-frame').evaluate(frame=>{const shape=n=>{const r=n.getBoundingClientRect();return [r.x,r.y,r.width,r.height];};return [...frame.children].map(shape);});assert(bounds.every(box=>JSON.stringify(box)===JSON.stringify(bounds[0])));
 const raw=await page.evaluate(async()=>{const {decodeMaskPixels}=await import('/mask-pixels.mjs');const data=await (await fetch('/fixture.json')).json();const ref=data.image.masks.prediction.mask;const url=URL.createObjectURL(new Blob([Uint8Array.from(atob(data.files[ref.path]),c=>c.charCodeAt(0))],{type:'image/png'}));try{const mask=await decodeMaskPixels(url);return [...new Set(mask.data.filter((_,i)=>i%4===0))].sort((a,b)=>a-b);}finally{URL.revokeObjectURL(url);}});assert.deepEqual(raw,[0,255]);
 await page.locator('.annotation-toggle input').last().uncheck();assert(await page.locator('canvas.annotation-overlay').isHidden());await page.locator('.annotation-toggle input').last().check();
 await page.locator('input[type=range]').evaluate(node=>{node.value='.7';node.dispatchEvent(new Event('input',{bubbles:true}));});assert.equal(await page.locator('canvas.annotation-overlay').evaluate(n=>n.style.opacity),'0.7');
 await mkdir(join(root,'test-results'),{recursive:true});await page.screenshot({path:join(root,'test-results/annotations-sdk-desktop.png'),fullPage:true});
 await page.setViewportSize({width:390,height:700});assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));await page.screenshot({path:join(root,'test-results/annotations-sdk-mobile.png'),fullPage:true});
 assert.deepEqual(errors,[]);assert.deepEqual(external,[]);assert.equal(await page.locator('.notice.error').count(),0);
 const result={sdkAnnotations:true,boxCoordinates:true,maskClass255:true,exactClassIds:true,noCanvasReadback:true,labelRetention:true,toggles:true,opacity:true,mobile:true,pageErrors:0,externalRequests:0};await writeFile(join(root,'test-results/annotations-browser.json'),JSON.stringify(result,null,2)+'\n');console.log(JSON.stringify(result));
}finally{await browser.close();await new Promise(resolve=>server.close(resolve));}
