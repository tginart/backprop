/**
 * Architecture: genuine existing Figure serializer -> isolated native renderer
 * -> selection-stage variants -> retained drawn geometry and actual exceptions.
 * Main caller: explicit focused source investigation, never Node discovery.
 * Why this exists: distinguish a public relayout repair from suppressing a
 * native selection exception or merely changing final fullData values.
 */
import {spawnSync} from 'node:child_process';
import {createServer} from 'node:http';
import {readFile,writeFile} from 'node:fs/promises';
import {fileURLToPath} from 'node:url';
import {join} from 'node:path';
import assert from 'node:assert/strict';
const root=fileURLToPath(new URL('../',import.meta.url));
const py=spawnSync(process.env.BACKPROP_PYTHON||'python3',['-B','-c',"import sys,json;sys.path[:0]=['sdk','tests'];import backprop as w;from plotly_animation_branch_figures import branch_figures;from pathlib import Path;print(Path(w.Plotly(branch_figures()['branch_layout_ranges_styles'])._path).read_text())"],{cwd:root,encoding:'utf8'});
assert.equal(py.status,0,py.stderr);const spec=JSON.parse(py.stdout);
const server=createServer(async(req,res)=>{try{res.setHeader('Content-Type',req.url==='/'?'text/html':'text/javascript');res.end(req.url==='/'?'<script src="/plotly-strict-4.1.1.min.js"></script>':await readFile(join(root,'web',req.url)));}catch{res.statusCode=404;res.end();}});
await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
const playwright=await import(process.env.PLAYWRIGHT_MODULE||'playwright');
const engine=process.env.PLOTLY_ENGINE||'chromium',launch={headless:true};if(engine==='chromium'&&process.platform==='darwin')launch.executablePath='/Applications/Brave Browser.app/Contents/MacOS/Brave Browser';
const browser=await playwright[engine].launch(launch),results=[];
try{
 for(const variant of ['original','no-selections','deferred','events-sequence','events-zero']){
  const page=await browser.newPage(),errors=[];page.on('pageerror',e=>errors.push(e.message));await page.goto('http://127.0.0.1:'+server.address().port);await page.waitForFunction(()=>window.Plotly);
  const result=await page.evaluate(async({spec,variant})=>{
   const graph=document.createElement('div');document.body.append(graph);const figure=structuredClone(spec),events=[],exceptions=[],operations=[];window.addEventListener('unhandledrejection',event=>exceptions.push({name:event.reason?.name,message:event.reason?.message,stack:event.reason?.stack}));
   if(variant==='no-selections'){figure.layout.selections=[];figure.frames[0].layout.selections=[];}
   await Plotly.newPlot(graph,figure.data,figure.layout,{displayModeBar:false});await Plotly.addFrames(graph,figure.frames);
   const shape=()=>({points:[...graph.querySelectorAll('.scatterlayer .point')].map(x=>x.getAttribute('transform')),shapes:[...graph.querySelectorAll('.shapelayer path')].map(x=>({d:x.getAttribute('d'),stroke:x.style.stroke,width:x.style.strokeWidth})),selections:[...graph.querySelectorAll('.selectionlayer path')].map(x=>({d:x.getAttribute('d'),stroke:x.style.stroke,width:x.style.strokeWidth})),ranges:[graph._fullLayout.xaxis.range,graph._fullLayout.yaxis.range],y:Array.from(graph._fullData[0].y),current:graph._fullLayout._currentFrame});
   const before=shape();let desired=structuredClone(figure.layout.selections||[]),last=Promise.resolve(),staged;
   if(variant.startsWith('events')){
    graph.on('plotly_animatingframe',event=>{if(event.frame.layout?.selections!==undefined)desired=structuredClone(event.frame.layout.selections);staged={layout:event.frame.layout,selections:event.frame.layout?.selections};if(event.frame.layout)event.frame.layout.selections=[];graph.layout.selections=[];events.push({phase:'frame',name:event.name,time:performance.now(),duration:event.animation.frame.duration});});
    graph.on('plotly_transitioning',()=>{graph.layout.selections=structuredClone(desired);if(staged?.layout){if(staged.selections===undefined)delete staged.layout.selections;else staged.layout.selections=staged.selections;}events.push({phase:'transitioning',time:performance.now()});});
    graph.on('plotly_transitioned',()=>{operations.push({phase:'applied',time:performance.now(),shape:shape()});});
    const base={name:'base',data:structuredClone(spec.data),layout:structuredClone(spec.layout)};delete base.layout.updatemenus;await Plotly.addFrames(graph,[base]);
   }
   if(variant==='deferred'){desired=structuredClone(figure.frames[0].layout.selections);graph.layout.selections=[];graph._transitionData._frameHash.next.layout.selections=[];}
   const duration=variant==='events-zero'?0:1100,options={mode:'immediate',frame:{duration,redraw:true},transition:{duration:variant==='events-zero'?0:900,easing:'linear'}};
   const promise=Plotly.animate(graph,variant.startsWith('events')?['next','base','next']:['next'],options);let outcome;
   await Promise.race([Promise.resolve(promise).then(()=>outcome='resolved',e=>outcome='rejected '+e?.message),new Promise(resolve=>setTimeout(resolve,variant==='original'?1400:5500))]);
   if(variant==='deferred'&&outcome==='resolved')await Plotly.relayout(graph,{selections:desired});await last;await new Promise(resolve=>setTimeout(resolve,80));
   return{variant,outcome,before,after:shape(),events,exceptions,operations,renderer:Plotly.version,plots:Object.keys(Plotly.Plots)};
  },{spec,variant});result.transport=errors;results.push(result);console.log(JSON.stringify(result));await page.close();
 }
 await writeFile(process.env.PLOTLY_LAYOUT_PROBE_OUTPUT||'/tmp/backprop-batch51-layout-probe.json',JSON.stringify(results,null,2)+'\n');
}finally{await browser.close();await new Promise(resolve=>server.close(resolve));}
