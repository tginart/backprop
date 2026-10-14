/**
 * Architecture: actual SDK Run history under release CSP -> original native
 * failure capture -> repaired viewer native controls -> independent static
 * golden SVG/pixels -> reset, retained updates and disposal. No timed-easing
 * claim follows from these finite behavior checks. Main caller: explicit local
 * browser command and parent-owned Firefox/WebKit CI setup.
 * Why this exists: the native broken animation is a source-negative oracle;
 * an independent ordinary newPlot is the positive final-geometry oracle.
 */
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
import {createServer} from 'node:http';
import {readFile,writeFile,mkdir} from 'node:fs/promises';
import {join,extname} from 'node:path';
import {fileURLToPath} from 'node:url';
import {branchShape} from './plotly_animation_branches.mjs';
const root=fileURLToPath(new URL('../',import.meta.url)),python=process.env.BACKPROP_PYTHON||'python3';
const fixture=spawnSync(python,['-B',join(root,'tests/plotly_layout_fixtures.py')],{encoding:'utf8',cwd:root,env:{...process.env,PYTHONPATH:join(root,'sdk')},maxBuffer:4*1024*1024});
assert.equal(fixture.status,0,fixture.stderr);const values=JSON.parse(fixture.stdout);
const html='<!doctype html><html><head><meta charset="utf-8"><link rel="stylesheet" href="/styles.css"></head><body><main style="display:flex;gap:24px;padding:12px"><section id="viewer"></section><section id="reference"></section></main><script type="module" src="/fixture.mjs"></script></body></html>';
const policy=spawnSync(python,['-B','-c','import sys;sys.path.insert(0,sys.argv[1]);from deploy import apply_content_security_policy;sys.stdout.buffer.write(apply_content_security_policy(sys.stdin.buffer.read(),{"host":"https://foundry-test.invalid"}))',join(root,'deploy')],{input:html,encoding:'utf8'});assert.equal(policy.status,0,policy.stderr);
const script=`import {hydratePlotly} from '/plotly-view.mjs';import {replaceView} from '/media-view.mjs';import {waitForPlotlyDrawing} from '/test/plotly_animation_settle.mjs';
window.fixture=${JSON.stringify(values)};window.registry=new Map();window.shape=(${branchShape.toString()});window.drawn=async nodes=>waitForPlotlyDrawing(nodes,window.shape);window.actualExceptions=[];
const capture=event=>{const error=event.type==='unhandledrejection'?event.reason:event.error;window.actualExceptions.push({source:event.type,name:error?.name,message:error?.message,stack:error?.stack});};for(const type of ['error','unhandledrejection'])window.addEventListener(type,capture);
const NativeObserver=ResizeObserver;window.activeObservers=0;window.ResizeObserver=class extends NativeObserver{observe(node){if(!this.active){this.active=true;window.activeObservers++;}super.observe(node);}disconnect(){if(this.active){this.active=false;window.activeObservers--;}super.disconnect();}};
window.clear=()=>{replaceView(document.querySelector('#viewer'),'');if(window.rawGraph)Plotly.purge(window.rawGraph);window.rawGraph=null;document.querySelector('#reference').replaceChildren();window.registry.clear();};
window.show=name=>{window.clear();window.registry.set(name,{id:name,...window.fixture.values[name]});replaceView(document.querySelector('#viewer'),'<div data-plotly-id="'+name+'" data-plotly-key="'+name+'"></div>');hydratePlotly(document.querySelector('#viewer'),window.registry);};
window.update=(name,index)=>{window.registry.set(name,{id:name,...window.fixture.retained[index]});hydratePlotly(document.querySelector('#viewer'),window.registry);};
window.reference=async(name,reset=false)=>{if(window.rawGraph)Plotly.purge(window.rawGraph);const graph=document.createElement('div');document.querySelector('#reference').replaceChildren(graph);const spec=structuredClone(window.fixture.values[name].spec);if(reset){spec.layout.xaxis.range=[-1,8];spec.layout.yaxis.range=[-1,10];}await Plotly.newPlot(graph,spec.data,spec.layout,{responsive:false,displayModeBar:false,displaylogo:false});window.rawGraph=graph;await window.drawn([graph]);return window.shape(graph);};
window.ready=true;`;
const server=createServer(async(req,res)=>{try{const name=new URL(req.url,'http://localhost').pathname;if(name.includes('..'))throw Error('invalid path');const body=name==='/'?policy.stdout:name==='/fixture.mjs'?script:name==='/test/plotly_animation_settle.mjs'?await readFile(join(root,'tests/plotly_animation_settle.mjs')):await readFile(join(root,'web',name));res.setHeader('Content-Type',name==='/'?'text/html':extname(name)==='.css'?'text/css':'text/javascript');res.end(body);}catch{res.statusCode=404;res.end();}});
await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));const origin='http://127.0.0.1:'+server.address().port;
const playwright=await import(process.env.PLAYWRIGHT_MODULE||'playwright');
const engine=process.env.PLOTLY_ENGINE||'chromium',launch={headless:true};assert(['chromium','firefox','webkit'].includes(engine));if(engine==='chromium'&&process.platform==='darwin')launch.executablePath=process.env.CHROMIUM_EXECUTABLE||'/Applications/Brave Browser.app/Contents/MacOS/Brave Browser';
const browser=await playwright[engine].launch(launch),output=process.env.PLOTLY_LAYOUT_OUTPUT||join(root,'test-results','plotly-layout-'+engine);await mkdir(output,{recursive:true});
const record={portable_behavior_passed:false,engine,browser:await browser.version(),renderer:'4.1.1',source_negatives:[],cases:[],sequences:[],retained:[],screenshots:[],errors:[],external:[],csp:[],timing:'Intermediate easing parity remains partial/unverified; this batch verifies finite layout drawing and native sequencing.'};
let page;
const geometry=shape=>{const result=structuredClone(shape);delete result.current;return result;};
const button=(graph,label)=>graph.locator('g.updatemenu-button').filter({hasText:new RegExp('^'+label+'$')});
async function ready(){await page.waitForFunction(()=>document.querySelector('#viewer [data-plotly-ready]'));assert.equal(await page.locator('#viewer .plotly-graph').count(),1,await page.locator('#viewer').innerText());await page.evaluate(()=>window.drawn([document.querySelector('#viewer .plotly-graph')]));}
async function completed(){assert.equal(await page.evaluate(()=>Promise.race([window.completed.then(()=>true),new Promise(resolve=>setTimeout(()=>resolve(false),12000))])),true,'native frame queue completes within the bounded fixture window');}
async function click(graph,label){record.phase=label;await graph.evaluate(node=>{window.completed=new Promise(resolve=>node.once('plotly_animated',resolve));window.controlEvents=[];window.frameEvents=[];node.once('plotly_buttonclicked',event=>window.controlEvents.push({label:event.button.label,args:structuredClone(event.button.args),execute:event.button.execute}));const observe=event=>window.frameEvents.push({name:event.name,frame:structuredClone(event.animation.frame),transition:structuredClone(event.animation.transition)});node.on('plotly_animatingframe',observe);window.stopObserving=()=>node.removeListener('plotly_animatingframe',observe);});await button(graph,label).click();await completed();await page.evaluate(async()=>{const node=document.querySelector('#viewer .plotly-graph');await window.drawn([node]);window.stopObserving();});return page.evaluate(()=>({controls:window.controlEvents,frames:window.frameEvents,actual:window.shape(document.querySelector('#viewer .plotly-graph'))}));}
async function compare(graph,golden,reset=false){const expected=await page.evaluate(({golden,reset})=>window.reference(golden,reset),{golden,reset}),actual=await graph.evaluate(node=>window.shape(node));record.last_comparison={golden,reset,actual,expected};assert.deepEqual(geometry(actual),geometry(expected),golden+' exact independently rendered geometry/data');assert.deepEqual(await page.locator('#viewer [role="alert"]').allTextContents(),[]);assert.deepEqual(await page.evaluate(()=>window.actualExceptions),[]);return actual;}
async function screenshot(graph,name){const path=join(output,name+'.png'),png=await graph.screenshot({path});const pixels=await page.evaluate(async encoded=>{const image=new Image();image.src='data:image/png;base64,'+encoded;await image.decode();const canvas=document.createElement('canvas');canvas.width=image.width;canvas.height=image.height;const context=canvas.getContext('2d');context.drawImage(image,0,0);const bytes=context.getImageData(0,0,canvas.width,canvas.height).data;let magenta=0,cyan=0;for(let index=0;index<bytes.length;index+=4){const [r,g,b]=bytes.slice(index,index+3);if(r>60&&b>60&&r-g>30&&b-g>30)magenta++;if(g>60&&b>60&&g-r>30&&b-r>30)cyan++;}return{magenta,cyan};},png.toString('base64'));assert(pixels.magenta>10,name+' actual magenta trace pixels');record.screenshots.push({path,pixels});return pixels;}
try{
 // Each source failure owns a fresh document, so an upstream rejected timer
 // cannot poison a later viewer or native source observation.
 for(const name of ['ranges_styles','ranges','styles']){
  const source=await browser.newPage({viewport:{width:1500,height:800}}),transport=[];source.on('pageerror',error=>transport.push(error.message));await source.goto(origin);await source.waitForFunction(()=>window.ready);
  await source.addScriptTag({url:origin+'/plotly-strict-4.1.1.min.js'});
  const before=await source.evaluate(async name=>{const spec=structuredClone(window.fixture.values[name].spec),graph=document.createElement('div');document.querySelector('#reference').append(graph);await Plotly.newPlot(graph,spec.data,spec.layout,{responsive:false,displayModeBar:false});await Plotly.addFrames(graph,spec.frames);window.rawGraph=graph;window.completed=new Promise(resolve=>graph.once('plotly_animated',resolve));return window.shape(graph);},name);
  await button(source.locator('#reference > div'),'Next').click();await source.evaluate(()=>window.completed);const native=await source.evaluate(async()=>{await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));return{after:window.shape(window.rawGraph),exceptions:window.actualExceptions,renderer:Plotly.version};});
  const row={name,before,...native,transport,screenshot:join(output,name+'-native-broken.png')};record.source_negatives.push(row);assert.deepEqual(native.after.paths,before.paths,name+' original native stale SVG');assert.deepEqual(native.after.data[0].y,[3,6]);assert.equal(native.exceptions.length,1);assert.equal(native.exceptions[0].source,'unhandledrejection');assert.equal(native.exceptions[0].name,'TypeError');assert(native.exceptions[0].stack.includes('/plotly-strict-4.1.1.min.js:'));assert(transport.length<=1);for(const message of transport)assert(message===native.exceptions[0].message||message===native.exceptions[0].name+': '+native.exceptions[0].message);await source.locator('#reference > div').screenshot({path:row.screenshot});await source.close();
 }
 page=await browser.newPage({viewport:{width:1600,height:900}});page.on('pageerror',error=>record.errors.push(error.message));page.on('request',request=>{if(/^https?:/.test(request.url())&&!request.url().startsWith(origin))record.external.push(request.url());});await page.addInitScript(()=>document.addEventListener('securitypolicyviolation',event=>(window.violations??=[]).push({directive:event.violatedDirective,blocked:event.blockedURI})));
 await page.goto(origin);await page.waitForFunction(()=>window.ready);
 for(const name of values.names){
  await page.evaluate(name=>window.show(name),name);await ready();const graph=page.locator('#viewer .plotly-graph');await compare(graph,name+'_base');
  const next=await click(graph,'Next');await compare(graph,name+'_next');assert.equal(next.controls[0].execute,false);assert.equal(next.frames.length,1);assert.equal(next.frames[0].frame.duration,360);assert.equal(next.frames[0].transition.duration,180);assert.equal(next.frames[0].transition.easing,'linear');
  const pixels=await screenshot(graph,name+'-next');if(['ranges_styles','styles'].includes(name))assert(pixels.cyan>10,name+' actual cyan shape and selection pixels');
  const back=await click(graph,'Back');await compare(graph,name+'_base');assert.equal(back.frames[0].name,'base');record.cases.push({name,next,back,pixels});console.log('Verified layout '+name);
 }
 await page.evaluate(()=>window.show('ranges_styles'));await ready();const graph=page.locator('#viewer .plotly-graph');
 for(const label of ['Sequence','Zero']){const observed=await click(graph,label);assert.deepEqual(observed.frames.map(frame=>frame.name),['next','base','next']);await compare(graph,'ranges_styles_next');record.sequences.push({label,...observed});console.log('Verified layout '+label);await click(graph,'Back');}
 // Actual native slider rail pointer input follows the same selection adapter.
 await graph.evaluate(node=>{window.completed=new Promise(resolve=>node.once('plotly_animated',resolve));});const rail=graph.locator('.slider-rail-touch-rect'),bounds=await rail.boundingBox();await rail.click({position:{x:bounds.width-3,y:8}});await completed();await page.evaluate(()=>window.drawn([document.querySelector('#viewer .plotly-graph')]));await compare(graph,'ranges_styles_next');record.slider=true;
 await page.locator('#viewer .plotly-actions').getByRole('button',{name:'Reset axes',exact:true}).click();await page.waitForFunction(()=>JSON.stringify(document.querySelector('#viewer .plotly-graph')._fullLayout.xaxis.range)==='[-1,8]');await page.evaluate(()=>window.drawn([document.querySelector('#viewer .plotly-graph')]));await compare(graph,'ranges_styles_next',true);await screenshot(graph,'reset-axes');record.reset=true;
 await graph.evaluate(node=>window.retainedNode=node);
 for(let index=0;index<values.retained.length;index++){
  // An identical descriptor retains its already displayed frame and viewport.
  // Only a changed signature invokes native react and resets saved trace data.
  await page.evaluate(index=>window.update('ranges_styles',index),index);await page.waitForFunction(index=>{const node=document.querySelector('#viewer .plotly-graph'),frame=node?._transitionData?._frameHash.next;return node===window.retainedNode&&frame&&JSON.stringify(frame.data[0].y)===JSON.stringify(index%2?[4,8]:[3,6])&&JSON.stringify(node._fullData[0].y)===JSON.stringify(index===0?[3,6]:[2,4]);},index);
  const observed=await click(graph,'Next');await compare(graph,index%2?'retained_golden':'ranges_styles_next');record.retained.push({index,duplicate_retention:index===0,same_node:await graph.evaluate(node=>node===window.retainedNode),...observed});
 }
 const savedBytes=JSON.stringify(values);assert.equal(await page.evaluate(()=>JSON.stringify(window.fixture)),savedBytes,'logged descriptors and saved frames remain unchanged');
 await click(graph,'Back');await button(graph,'Next').click();await page.waitForFunction(()=>Boolean(document.querySelector('#viewer .plotly-graph')._transitionData._animationRaf));
 record.active_cleanup=await graph.evaluate(node=>({current:node._fullLayout._currentFrame,queue_active:Boolean(node._transitionData._animationRaf),selection_inputs:structuredClone(node.layout.selections)}));
 await page.evaluate(()=>window.clear());await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));assert.equal(await page.evaluate(()=>window.activeObservers),0);assert.equal(await page.locator('#viewer .plotly-graph').count(),0);assert.deepEqual(await page.evaluate(()=>window.actualExceptions),[]);record.cleanup=true;
 // Remove in the event itself, before asynchronous native prepare can set its
 // duration flag. An active RAF queue is already present at this boundary.
 await page.evaluate(()=>window.show('ranges_styles'));await ready();
 await graph.evaluate(node=>node.once('plotly_animatingframe',event=>{
  window.earlyDisposedNode=node;window.earlyDisposedFrame=event.frame;window.earlyDisposedLayout=node.layout;
  window.earlyDisposePhase={transitioning:Boolean(node._transitioning),duration:Boolean(node._transitioningWithDuration),queue_active:Boolean(node._transitionData?._animationRaf),staged_selections:structuredClone(node.layout.selections),staged_frame:structuredClone(event.frame.layout)};
  window.clear();
 }));
 await button(graph,'Next').click();await page.waitForFunction(()=>window.earlyDisposedNode&&!window.earlyDisposedNode._fullLayout);
 await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
 record.prepare_cleanup=await page.evaluate(()=>({phase:window.earlyDisposePhase,restored_frame:structuredClone(window.earlyDisposedFrame.layout),restored_selections:structuredClone(window.earlyDisposedLayout.selections),listeners:window.earlyDisposedNode.eventNames?.()||[],purged:!window.earlyDisposedNode._fullLayout,active_observers:window.activeObservers}));
 assert.equal(record.prepare_cleanup.phase.transitioning,false);assert.equal(record.prepare_cleanup.phase.duration,false);assert.equal(record.prepare_cleanup.phase.queue_active,true);assert.deepEqual(record.prepare_cleanup.phase.staged_selections,[]);
 assert.deepEqual(record.prepare_cleanup.restored_frame.selections,values.values.ranges_styles.spec.frames.find(frame=>frame.name==='next').layout.selections);
 assert.deepEqual(record.prepare_cleanup.restored_selections,values.values.ranges_styles_next.spec.layout.selections);
 assert.equal(record.prepare_cleanup.purged,true);assert.equal(record.prepare_cleanup.active_observers,0);assert.deepEqual(record.prepare_cleanup.listeners,[]);assert.deepEqual(await page.evaluate(()=>window.actualExceptions),[]);assert.equal(await page.locator('#viewer .plotly-graph').count(),0);
 record.csp=await page.evaluate(()=>window.violations||[]);assert.deepEqual(record.errors,[]);assert.deepEqual(record.external,[]);assert.deepEqual(record.csp,[]);record.portable_behavior_passed=true;delete record.last_comparison;
 await writeFile(join(output,'summary.json'),JSON.stringify(record,null,2)+'\n');console.log(JSON.stringify({portable_behavior_passed:true,cases:record.cases.length,source_negatives:record.source_negatives.length,sequences:record.sequences.length,retained:record.retained.length,slider:record.slider,reset:record.reset,cleanup:record.cleanup,errors:record.errors,external:record.external,csp:record.csp,output}));
}catch(error){record.error=String(error);if(page)record.diagnostic=await page.evaluate(()=>{const node=document.querySelector('#viewer .plotly-graph');return{actual_exceptions:window.actualExceptions,alerts:[...document.querySelectorAll('#viewer [role="alert"]')].map(node=>node.innerText),shape:node?window.shape(node):null,controls:window.controlEvents,frames:window.frameEvents,queue:node?._transitionData?._frameQueue?.map(frame=>({name:frame.name,frame:frame.frameOpts,transition:frame.transitionOpts})),transitioning:node?._transitioning,active_observers:window.activeObservers};});await writeFile(join(output,'failure.json'),JSON.stringify(record,null,2)+'\n');throw error;}
finally{await browser.close();await new Promise(resolve=>server.close(resolve));}
