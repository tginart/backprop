/** Graph viewer QA with actual SDK bytes and the deployed content policy.
 * The browser is isolated, and generated files/screenshots remain in /tmp.
 * Main caller: node tests/browser_graph_qa.mjs using installed Playwright/Brave.
 */
import assert from 'node:assert/strict';
import {createServer} from 'node:http';
import {readFile,mkdtemp} from 'node:fs/promises';
import {join} from 'node:path';
import {tmpdir} from 'node:os';
import {fileURLToPath} from 'node:url';
import {spawnSync} from 'node:child_process';
const root=fileURLToPath(new URL('../',import.meta.url));
const {chromium}=await import(process.env.PLAYWRIGHT_MODULE || 'playwright');
const generated=spawnSync(process.env.BACKPROP_PYTHON || 'python3',['-B','-c',`
import json,tempfile
import backprop
from backprop.transport import MemorySink
with tempfile.TemporaryDirectory() as directory:
 sink=MemorySink()
 run=backprop.init(project="graph-browser",dir=directory,_sink=sink,settings={"console":"off","silent":True,"x_disable_stats":True})
 graph=backprop.Graph()
 a=graph.add_node(id="input",name="Input",class_name="InputLayer",output_shape=[None,32])
 b=graph.add_node(id="hidden",name="Dense <img src=x onerror=alert(1)>",class_name="Dense",num_parameters=1056,output_shape=[None,32])
 c=graph.add_node(id="output",name="Prediction",class_name="Dense",num_parameters=33,output_shape=[None,1])
 graph.add_edge(a,b);graph.add_edge(b,c)
 b._attributes.update(nonfinite=float('nan'),positive=float('inf'),negative=-float('inf'))
 run.log({"architecture":graph});run.finish()
 event=next(e for e in sink.events if e['type']=='history')
 print(json.dumps({"graph":next(data for data in sink.files.values() if b'"nodes"' in data).decode()}))
`],{cwd:root,env:{...process.env,PYTHONPATH:join(root,'sdk')},encoding:'utf8'});
assert.equal(generated.status,0,generated.stderr);const graph=JSON.parse(generated.stdout).graph;
const html='<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/styles.css"></head><body><main style="padding:20px;max-width:1100px"><h1>Model architecture</h1><div id="graph"></div><div id="incomplete"></div></main><script type="module" src="/fixture.mjs"></script></body></html>';
const policy=spawnSync(process.env.PYTHON || 'python3',['-B','-c','import sys;sys.path.insert(0,sys.argv[1]);from deploy import apply_content_security_policy;sys.stdout.buffer.write(apply_content_security_policy(sys.stdin.buffer.read(),{"host":"https://foundry-test.invalid"}))',join(root,'deploy')],{input:html,encoding:'utf8'});assert.equal(policy.status,0,policy.stderr);
const fixture=`import {graphViewer} from '/graph-view.mjs';import {parseGraphJSON} from '/graph-json.mjs';document.querySelector('#graph').append(graphViewer(parseGraphJSON(${JSON.stringify(graph)})));document.querySelector('#incomplete').append(graphViewer({format:'torch',nodes:[],edges:[]},{capture:{complete:false,missing_modules:['conditional.head']}}));window.ready=true;`;
const server=createServer(async(req,res)=>{try{const path=new URL(req.url,'http://localhost').pathname;const body=path==='/'?policy.stdout:path==='/fixture.mjs'?fixture:await readFile(join(root,'web',path));res.setHeader('Content-Type',path==='/'?'text/html':path.endsWith('.mjs')?'text/javascript':path.endsWith('.css')?'text/css':'application/json');res.end(body);}catch{res.writeHead(404);res.end();}});
await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));const url=`http://127.0.0.1:${server.address().port}`;
const browser=await chromium.launch({executablePath:process.env.BROWSER_EXECUTABLE,headless:true});
try {
 const page=await browser.newPage({viewport:{width:1100,height:850}}),errors=[],external=[];
 page.on('pageerror',error=>errors.push(error.message));page.on('request',req=>{if(/^https?:/.test(req.url())&&!req.url().startsWith(url))external.push(req.url());});
 await page.goto(url);await page.waitForFunction(()=>window.ready);
 assert.equal(await page.locator('.graph-node').count(),3);assert.equal(await page.locator('.graph-edge').count(),2);
 await page.getByLabel('Inspect graph node').first().selectOption('1');
 assert((await page.locator('.graph-inspector').innerText()).includes('1056'));
 for(const value of ['NaN','Infinity','-Infinity'])assert((await page.locator('.graph-inspector').innerText()).includes(value));
 assert.equal(await page.locator('.graph-viewer img').count(),0);
 await page.locator('.graph-node').nth(2).focus();await page.keyboard.press('Enter');
 assert((await page.locator('.graph-inspector').innerText()).includes('Prediction'));
 assert((await page.locator('.graph-incomplete').innerText()).includes('conditional.head'));
 const output=await mkdtemp(join(tmpdir(),'backprop-graph-qa-'));
 await page.screenshot({path:join(output,'desktop.png'),fullPage:true});
 await page.setViewportSize({width:390,height:844});
 assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
 await page.screenshot({path:join(output,'mobile.png'),fullPage:true});
 assert.deepEqual(errors,[]);assert.deepEqual(external,[]);
 console.log(JSON.stringify({nodes:3,edges:2,nonfinite:true,keyboard:true,mobile:true,incomplete:true,externalRequests:0,screenshots:output}));
} finally {await browser.close();await new Promise(resolve=>server.close(resolve));}
