/** Synthetic packaged-app startup/refresh failures, with genuine browser fetch cancellation. */
import assert from 'node:assert/strict';
import {createServer} from 'node:http';
import {readFile} from 'node:fs/promises';
import {fileURLToPath} from 'node:url';
import {join,extname} from 'node:path';
const root=fileURLToPath(new URL('../',import.meta.url));
const {chromium}=await import(process.env.PLAYWRIGHT_MODULE || 'playwright');
const config={host:'https://foundry.example.com',client_id:'synthetic-public-client',app_url:'https://backprop.example.com/',stream_dataset_rid:'ri.foundry.main.dataset.events',media_dataset_rid:'ri.foundry.main.dataset.media'};
const server=createServer(async(req,res)=>{
 try {
   const path=new URL(req.url,'http://localhost').pathname;
   const body=path==='/config.json'?JSON.stringify(config):await readFile(join(root,'web',path==='/'?'index.html':path));
   res.setHeader('Content-Type',path==='/'?'text/html':extname(path)==='.css'?'text/css':['.js','.mjs'].includes(extname(path))?'text/javascript':'application/json');res.end(body);
 }catch{res.writeHead(404);res.end();}
});
await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
const browser=await chromium.launch({headless:true,...(process.env.CHROMIUM_EXECUTABLE?{executablePath:process.env.CHROMIUM_EXECUTABLE}:{})});
try {
 const page=await browser.newPage();const origin=`http://127.0.0.1:${server.address().port}`;let phase='startup-stall';
 await page.addInitScript(()=>{
   const timer=globalThis.setTimeout;
   globalThis.setTimeout=(fn,ms,...args)=>timer(fn,[30000,180000,240000,5000].includes(ms)?1000:ms,...args);
   sessionStorage.setItem('backprop.oauth.session',JSON.stringify({accessToken:'synthetic',expiresAt:Date.now()+3600000}));
 });
 await page.route('**/config.json',route=>phase==='startup-stall'?new Promise(()=>{}):route.continue());
 await page.route('https://foundry.example.com/**',route=>{
   const url=route.request().url();
   if(phase==='refresh-stall')return new Promise(()=>{});
   if(url.endsWith('/execute'))return route.fulfill({json:{queryId:'synthetic',type:'succeeded'}});
   if(url.endsWith('/getResults'))return route.fulfill({body:'schema_version,event_id,run_id,event_type,timestamp,payload\n'});
   if(url.includes('/getEndOffsets'))return route.fulfill({json:{}});
   return route.fulfill({status:404});
 });
 await page.goto(origin);
 await page.getByText(/Request timed out/).first().waitFor();
 assert.equal(await page.locator('[aria-busy="true"]').count(),0);
 phase='ready';await page.locator('[data-action="refresh"]').first().click();
 await page.waitForFunction(()=>!document.body.textContent.includes('Request timed out') && document.querySelector('[data-action="refresh"]') && !document.querySelector('[data-action="refresh"][disabled]') && !document.querySelector('[aria-busy="true"]'));
 phase='refresh-stall';await page.locator('[data-action="refresh"]').first().click();
 await page.getByText(/Request timed out/).first().waitFor();
 await page.waitForFunction(()=>!document.querySelector('[data-action="refresh"][disabled]'));
 assert.equal(await page.locator('[data-action="refresh"][disabled]').count(),0);
 phase='ready';await page.locator('[data-action="refresh"]').first().click();
 await page.waitForFunction(()=>!document.body.textContent.includes('Request timed out'));
 console.log(JSON.stringify({startupTimeout:true,startupRetry:true,refreshTimeout:true,refreshRetry:true,busyCleanup:true}));
} finally {await browser.close();await new Promise(resolve=>server.close(resolve));}
