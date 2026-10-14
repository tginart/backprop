/** Genuine pinned Plotly under the release CSP, isolated installed browser only.
 * Architecture: local fixture -> real renderer + lifecycle -> geometry/security
 * assertions -> desktop/mobile screenshots. No logged-in profile or live data. */
import assert from 'node:assert/strict';
import {createServer} from 'node:http';
import {readFile,mkdir} from 'node:fs/promises';
import {join,extname} from 'node:path';
import {fileURLToPath} from 'node:url';
import {spawnSync} from 'node:child_process';
const root=fileURLToPath(new URL('../',import.meta.url));
const playwright=await import(process.env.PLAYWRIGHT_MODULE || 'playwright');
const html='<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/styles.css"></head><body><h1>Synthetic demo · Plotly validation</h1><main id="fixtures" style="padding:16px;display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,420px),1fr));gap:20px"></main><script type="module" src="/fixture.mjs"></script></body></html>';
const policy=spawnSync(process.env.PYTHON || 'python3',['-c','import sys;sys.path.insert(0,sys.argv[1]);from deploy import apply_content_security_policy;sys.stdout.buffer.write(apply_content_security_policy(sys.stdin.buffer.read(),{"host":"https://foundry-test.invalid"}))',join(root,'deploy')],{input:html,encoding:'utf8'});assert.equal(policy.status,0);
const fixtures={
 heat:{data:[{type:'heatmap',z:[[1,2],[3,4]],colorscale:[[0,'rgb(0,0,0)'],[1,'rgb(255,255,255)']],zmin:1,zmax:4}],layout:{title:{text:'Heatmap · saved color scale'},annotations:[{x:0,y:0,text:'<img src=x onerror="window.unsafe=true"><a href="javascript:alert(1)">safe text</a>',showarrow:false}]}},
 bars:{data:[{type:'bar',name:'Control',orientation:'h',y:['A','B'],x:[2,3]},{type:'bar',name:'Experiment',orientation:'h',y:['A','B'],x:[4,5]}],layout:{title:{text:'Horizontal stacked bars'},barmode:'stack'}},
 dates:{data:[{type:'scatter',x:['2026-01-01','2026-01-02','2026-01-04'],y:[1,2,4],name:'Dates'},{type:'bar',x:['A','B'],y:[2,5],xaxis:'x2',yaxis:'y2',name:'Other axis'}],layout:{title:{text:'Date axis and subplot'},grid:{rows:1,columns:2,pattern:'independent'},xaxis:{type:'date'}}},
 gaps:{data:[{type:'scatter',mode:'lines+markers',x:[0,1,2,3,4,5,6],y:[1,{_type:'number',value:'NaN'},3,{_type:'number',value:'Infinity'},5,6,7],name:'Finite samples'},{type:'scatter',x:[0,6],y:[2,5],name:'Reference',visible:'legendonly'}],layout:{title:{text:'Nonfinite gaps and legend controls'}}},
 typed:{data:[{type:'heatmap',z:{dtype:'f8',bdata:'AAAAAAAA8D8AAAAAAAAAQAAAAAAAAAhAAAAAAAAAEEA=',shape:'2, 2'},colorscale:[[0,'rgb(0,0,0)'],[1,'rgb(255,255,255)']],zmin:1,zmax:4}],layout:{title:{text:'Typed matrix'}}},
 external:{data:[{x:[0,1],y:[1,2]}],layout:{title:{text:'Uncaptured image: explicit resource error'},images:[{source:'https://never-request.invalid/data.png',x:0,y:1,sizex:1,sizey:1}]}}
};
const fixtureScript=`import {hydratePlotly} from '/plotly-view.mjs';import {replaceView} from '/media-view.mjs';const values=${JSON.stringify(fixtures)};const registry=new Map(Object.entries(values).map(([id,spec])=>[id,{id,spec}]));const root=document.querySelector('#fixtures');const html=()=>[...registry].map(([id,{spec}])=>'<section style="min-width:0;background:#151c19;padding:12px"><div data-plotly-id="'+id+'" data-plotly-key="'+id+'"><p>Loading...</p></div></section>').join('');window.refresh=()=>{replaceView(root,html());hydratePlotly(root,registry)};window.add=(id,spec)=>{registry.set(id,{id,spec});window.refresh()};window.change=()=>{registry.get('gaps').spec.data[0].y[0]=9;window.refresh()};window.remove=()=>replaceView(root,'');window.refresh();`;
const server=createServer(async(req,res)=> {try{let content,type='text/javascript';if(req.url==='/'){content=policy.stdout;type='text/html';}else if(req.url==='/fixture.mjs')content=fixtureScript;else{const name=req.url.slice(1);if(name.includes('..'))throw Error();content=await readFile(join(root,'web',name));if(extname(name)==='.css')type='text/css';}res.setHeader('Content-Type',type);res.end(content);}catch{res.statusCode=404;res.end();}});
await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
const browser=await playwright.chromium.launch({headless:true,executablePath:process.env.CHROMIUM_EXECUTABLE});
const page=await browser.newPage({viewport:{width:1500,height:1100}});const errors=[],external=[];
page.on('pageerror',error=>errors.push(error.message));page.on('request',req=>{if(!req.url().startsWith('http://127.0.0.1:')&&!req.url().startsWith('data:')&&!req.url().startsWith('blob:'))external.push(req.url());});
try{
 await page.goto(`http://127.0.0.1:${server.address().port}/`);await page.waitForFunction(()=>document.querySelectorAll('[data-plotly-ready]').length===6);
 assert(await page.locator('.modebar').evaluateAll(nodes=>nodes.every(node=>getComputedStyle(node).backgroundColor==='rgba(0, 0, 0, 0)')),'Floating toolbars must not paint opaque rectangles over titles');
 const graph=id=>page.locator(`[data-plotly-id="${id}"] .plotly-graph`);
 assert.equal(await graph('heat').locator('.heatmaplayer image').count(),1);
 const pixels=async locator=>locator.evaluate(async node=> {
   const image=new Image();image.src=node.getAttribute('href');await image.decode();
   const canvas=document.createElement('canvas');canvas.width=image.naturalWidth;canvas.height=image.naturalHeight;const ctx=canvas.getContext('2d');ctx.drawImage(image,0,0);
   return {width:canvas.width,height:canvas.height,pixels:Array.from(ctx.getImageData(0,0,canvas.width,canvas.height).data)};
 });
 const heatPixels=await pixels(graph('heat').locator('.heatmaplayer image'));
 assert.equal(heatPixels.width,2);assert.equal(heatPixels.height,2);
 const expectedPixels=[170,170,170,255,255,255,255,255,0,0,0,255,85,85,85,255];
 // Brave's canvas fingerprint protection perturbs RGB by approximately one
 // unit. This narrow tolerance still proves every decoded cell and orientation.
 heatPixels.pixels.forEach((value,index)=>assert(Math.abs(value-expectedPixels[index])<=2,`Heatmap channel ${index}: ${value}`));
 assert.equal(await graph('bars').locator('.barlayer .point path').count(),4);
 const bars=await graph('bars').evaluate(n=>n.calcdata.map(trace=>trace.map(p=>({s:p.s,b:p.b}))));
 assert.equal(bars[1][0].b,2);assert.equal(bars[1][1].b,3);
 assert.equal(await graph('dates').evaluate(n=>n._fullLayout.xaxis.type),'date');assert.equal(await graph('dates').locator('.subplot.xy').count(),1);assert.equal(await graph('dates').locator('.subplot.x2y2').count(),1);
 assert.equal(await graph('gaps').locator('.scatterlayer .point').count(),5);assert.equal(await graph('gaps').locator('.scatterlayer .js-line').count(),3);
 await page.getByRole('button',{name:'Show Reference',exact:true}).focus();await page.keyboard.press('Enter');await page.waitForFunction(()=>document.querySelector('[data-plotly-id="gaps"] .plotly-graph').data[1].visible===true);
 assert(await page.getByRole('button',{name:'Hide Reference',exact:true}).evaluate(n=>n===document.activeElement));
 await graph('gaps').evaluate(n=>window.Plotly.relayout(n,{'xaxis.range':[1,4]}));
 await graph('gaps').evaluate(n=>{window.priorGraph=n;});await page.evaluate(()=>{window.refresh();window.refresh();});
 assert(await graph('gaps').evaluate(n=>n===window.priorGraph));assert.deepEqual(await graph('gaps').evaluate(n=>n._fullLayout.xaxis.range),[1,4]);
 await page.evaluate(()=>window.change());await page.waitForFunction(()=>document.querySelector('[data-plotly-id="gaps"] .plotly-graph').data[0].y[0]===9);assert(await graph('gaps').evaluate(n=>n===window.priorGraph));assert.deepEqual(await graph('gaps').evaluate(n=>n._fullLayout.xaxis.range),[1,4]);
 assert.equal(await graph('gaps').evaluate(n=>n.data[1].visible),true);
 assert.equal(await graph('typed').locator('.heatmaplayer image').count(),1);const typedPixels=await pixels(graph('typed').locator('.heatmaplayer image'));assert.equal(typedPixels.width,2);assert.equal(typedPixels.height,2);typedPixels.pixels.forEach((value,index)=>assert(Math.abs(value-expectedPixels[index])<=2));
 assert((await page.locator('[data-plotly-id="external"]').innerText()).includes('not captured in Foundry'));
 const inheritedSource=await graph('heat').locator('.heatmaplayer image').getAttribute('href');
 await page.evaluate(source=>window.add('template',{data:[{type:'image'}],layout:{title:{text:'Image inherited from explicit template'},template:{data:{image:[{source}]}}}}),inheritedSource);
 await page.locator('[data-plotly-id="template"][data-plotly-ready]').waitFor();
 assert.equal(await graph('template').evaluate(n=>n._fullData[0].source),inheritedSource);assert.equal(await graph('template').locator('.imagelayer image').count(),1);
 assert.equal(await page.evaluate(()=>window.unsafe),undefined);assert.equal(await page.locator('a[href^="javascript:"]').count(),0);
 await graph('gaps').evaluate(n=>window.Plotly.Fx.hover(n,[{curveNumber:0,pointNumber:2}]));assert(await graph('gaps').locator('.hoverlayer').textContent());
 const output=join(root,'test-results');await mkdir(output,{recursive:true});await page.screenshot({path:join(output,'plotly-genuine-desktop.png'),fullPage:true});
 await page.setViewportSize({width:390,height:844});await graph('bars').scrollIntoViewIfNeeded();await page.waitForTimeout(300);assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));await page.screenshot({path:join(output,'plotly-genuine-mobile.png'),fullPage:false});
 await page.evaluate(()=>window.remove());assert.equal(await page.evaluate(()=>Boolean(window.priorGraph._fullLayout)),false);
 assert.deepEqual(external,[]);assert.deepEqual(errors,[]);console.log(JSON.stringify({passed:true,figures:7,external_requests:external.length,runtime_errors:errors.length}));
}finally{await browser.close();await new Promise(resolve=>server.close(resolve));}
