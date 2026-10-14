/**
 * Architecture: genuine SDK history -> release CSP -> existing pinned renderer
 * -> calculated values and screenshot pixels -> updates, errors and disposal.
 * Main callers: the combined parity browser pass. Installed Playwright/Brave are
 * used in a fresh isolated profile. No package, browser or service is installed.
 * Why this exists: a graph DOM/canvas placeholder cannot verify trace geometry,
 * especially binary coordinates, image matrices and the seven 3D trace families.
 */
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
import {createServer} from 'node:http';
import {readFile,mkdir,writeFile} from 'node:fs/promises';
import {join,extname} from 'node:path';
import {fileURLToPath} from 'node:url';

const root=fileURLToPath(new URL('../',import.meta.url));
const python=process.env.BACKPROP_PYTHON || 'python3';
const fixture=spawnSync(python,['-B',join(root,'tests/plotly_gap_replay_fixture.py')],{encoding:'utf8',cwd:root,env:{...process.env,PYTHONPATH:join(root,'sdk')},maxBuffer:8*1024*1024});
assert.equal(fixture.status,0,fixture.stderr);const data=JSON.parse(fixture.stdout);
const {chromium}=await import(process.env.PLAYWRIGHT_MODULE || 'playwright');
const html='<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/styles.css"></head><body><main id="fixtures" style="padding:16px"></main><script type="module" src="/fixture.mjs"></script></body></html>';
const policy=spawnSync(python,['-B','-c','import sys;sys.path.insert(0,sys.argv[1]);from deploy import apply_content_security_policy;sys.stdout.buffer.write(apply_content_security_policy(sys.stdin.buffer.read(),{"host":"https://foundry-test.invalid"}))',join(root,'deploy')],{input:html,encoding:'utf8'});
assert.equal(policy.status,0,policy.stderr);
const script=`import {hydratePlotly} from '/plotly-view.mjs';import {replaceView} from '/media-view.mjs';
const fixtures=${JSON.stringify(data.values)},root=document.querySelector('#fixtures'),registry=new Map();
const NativeObserver=globalThis.ResizeObserver;window.activeObservers=0;globalThis.ResizeObserver=class extends NativeObserver{observe(node){if(!this.active){this.active=true;window.activeObservers++;}super.observe(node);}disconnect(){if(this.active){this.active=false;window.activeObservers--;}super.disconnect();}};
window.show=(name)=>{registry.clear();registry.set(name,{id:name,...fixtures[name]});replaceView(root,'<section><div data-plotly-id="'+name+'" data-plotly-key="'+name+'">Loading...</div></section>');hydratePlotly(root,registry);};
window.refresh=()=>{const id=[...registry.keys()][0];replaceView(root,'<section><div data-plotly-id="'+id+'" data-plotly-key="'+id+'">Loading...</div></section>');hydratePlotly(root,registry);};
window.change=(edit)=>{const value=[...registry.values()][0];if(edit==='data'){value.spec.data[0].y=[3,5];value.spec.frames=[{name:'replacement',traces:[0],data:[{type:'scatter',x:[0,1],y:[2,3]}]}];}else if(edit==='invalid')value.spec.data[0].type='future_trace';else if(edit==='repair')value.spec.data[0].type='scatter';window.refresh();};
window.remove=()=>{window.disposedGraphs=[...root.querySelectorAll('.plotly-graph')];replaceView(root,'');};
window.ready=true;`;
const server=createServer(async(req,res)=> {
  try {
    const name=new URL(req.url,'http://localhost').pathname;
    if(name.includes('..'))throw new Error('Invalid fixture path');
    const body=name==='/'?policy.stdout:name==='/fixture.mjs'?script:await readFile(join(root,'web',name));
    res.setHeader('Content-Type',name==='/'?'text/html':extname(name)==='.css'?'text/css':name.endsWith('.json')?'application/json':'text/javascript');res.end(body);
  }catch {res.statusCode=404;res.end();}
});
await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
const origin=`http://127.0.0.1:${server.address().port}`;
const browser=await chromium.launch({headless:true,executablePath:process.env.CHROMIUM_EXECUTABLE,args:['--enable-unsafe-swiftshader']});
const page=await browser.newPage({viewport:{width:900,height:650}}),errors=[],external=[],violations=[],matrix=[];
page.on('pageerror',error=>errors.push(error.message));
page.on('request',request=>{if(/^https?:/.test(request.url())&&!request.url().startsWith(origin))external.push(request.url());});
await page.addInitScript(()=>document.addEventListener('securitypolicyviolation',event=>(window.policyViolations??=[]).push({directive:event.violatedDirective,blocked:event.blockedURI})));
const output=join(root,'test-results');await mkdir(output,{recursive:true});

/** Wait for the viewer's actual completion/error state, never a loading node.
 * Main callers: trace loop and error/lifecycle cases. The single retained chart
 * is disposed before the next family, keeping WebGL contexts and observers bound.
 */
async function show(name) {
  await page.evaluate(name=>window.show(name),name);
  await page.waitForFunction(name=>Boolean(document.querySelector('[data-plotly-id="'+name+'"][data-plotly-ready]')),name,{timeout:45000});
  return page.locator(`[data-plotly-id="${name}"] .plotly-graph`);
}

/** Count the specified trace color in actual captured browser pixels.
 * Main caller: every matrix case. Layout text, axes and backgrounds are gray;
 * only saved trace data is magenta. The broad channel range tolerates installed
 * Brave canvas fingerprint perturbations and antialiasing without accepting an
 * empty graph, a loading label, a toolbar or grayscale axis-only canvas.
 */
async function drawnPixels(graph,name) {
  const png=await graph.screenshot({path:join(output,`plotly-gap-${name}.png`)});
  return page.evaluate(async encoded=> {
    const image=new Image();image.src='data:image/png;base64,'+encoded;await image.decode();
    const canvas=document.createElement('canvas');canvas.width=image.naturalWidth;canvas.height=image.naturalHeight;
    const context=canvas.getContext('2d');context.drawImage(image,0,0);const values=context.getImageData(0,0,canvas.width,canvas.height).data;
    let magenta=0;for(let i=0;i<values.length;i+=4)if(values[i]>45&&values[i+2]>45&&values[i]-values[i+1]>15&&values[i+2]-values[i+1]>15&&values[i+3]>0)magenta++;
    return {width:canvas.width,height:canvas.height,magenta};
  },png.toString('base64'));
}

try {
  await page.goto(origin);await page.waitForFunction(()=>window.ready);
  const names=data.figure_names.filter(name=>!['typed_matrix','dates_gaps'].includes(name));
  for(const name of names) {
    const graph=await show(name);assert.equal(await graph.count(),1,await page.locator('#fixtures').innerText());
    const state=await graph.evaluate(node=>({types:node._fullData.map(trace=>trace.type),visible:node._fullData.map(trace=>trace.visible),calculated:node.calcdata?.length,frames:node._transitionData?._frames?.length || 0}));
    assert.deepEqual(state.types,data.values[name].spec.data.map(trace=>trace.type),name);
    assert(state.calculated>0,`${name}: the renderer must calculate its trace data`);
    const pixels=await drawnPixels(graph,name);
    assert(pixels.magenta>10,`${name}: saved trace data did not draw (${JSON.stringify(pixels)})`);
    assert.equal(await page.locator('#fixtures [role="alert"]').count(),0,`${name}: ${await page.locator('#fixtures').innerText()}`);
    matrix.push({name,...state,...pixels});
  }
  const registered=await page.evaluate(()=>Object.keys(Plotly.PlotSchema.get().traces).sort());
  assert.deepEqual(matrix.filter(row=>registered.includes(row.name)).map(row=>row.name).sort(),registered,'Every registered trace family needs an SDK fixture and drawn-color assertion');

  for(const dtype of ['i1','u1','i2','u2','i4','u4','f4','f8','i8','u8']) {
    const graph=await show('typed_'+dtype);const numbers=await graph.evaluate(node=>Array.from(node._fullData[0].x));assert.deepEqual(numbers,[0,1,2,3],dtype);
    assert.equal(await graph.locator('.scatterlayer .point').count(),4,dtype);
  }
  const heat=await show('typed_matrix');
  const pixels=await heat.locator('.heatmaplayer image').evaluate(async node=> {
    const image=new Image();image.src=node.getAttribute('href');await image.decode();
    const canvas=document.createElement('canvas');canvas.width=image.naturalWidth;canvas.height=image.naturalHeight;
    const context=canvas.getContext('2d');context.drawImage(image,0,0);
    return {width:canvas.width,height:canvas.height,values:Array.from(context.getImageData(0,0,canvas.width,canvas.height).data)};
  });
  assert.equal(pixels.width,2);assert.equal(pixels.height,2);
  [170,170,170,255,255,255,255,255,0,0,0,255,85,85,85,255].forEach((channel,index)=>assert(Math.abs(pixels.values[index]-channel)<=2,`SDK binary matrix channel ${index}`));
  matrix.push({name:'typed_matrix',...pixels});
  const gaps=await show('dates_gaps');assert.equal(await gaps.evaluate(node=>node._fullLayout.xaxis.type),'date');assert.equal(await gaps.locator('.scatterlayer .point').count(),2);
  matrix.push({name:'dates_gaps',points:2,date_axis:true});

  const animated=await show('animation');
  assert.equal(await animated.evaluate(node=>node._transitionData._frames[0].name),'next');
  assert((await animated.locator('.annotation-text').allTextContents()).includes('Saved annotation'));
  const beforePath=await animated.locator('.scatterlayer .js-line').getAttribute('d');
  await animated.getByText('Play',{exact:true}).click();
  await page.waitForFunction(()=>document.querySelector('.plotly-graph').data[0].y[0]===4);
  assert.notEqual(await animated.locator('.scatterlayer .js-line').getAttribute('d'),beforePath);
  assert.equal(await animated.locator('.gtitle').textContent(),'Animated title');
  await animated.evaluate(node=>{window.retained=node;});
  const drag=await animated.locator('.nsewdrag').boundingBox();
  await page.mouse.move(drag.x+drag.width*.2,drag.y+drag.height*.2);await page.mouse.down();
  await page.mouse.move(drag.x+drag.width*.7,drag.y+drag.height*.8,{steps:8});await page.mouse.up();
  await page.waitForFunction(()=>document.querySelector('.plotly-graph')._fullLayout.xaxis.range[0]!==-1);
  const retainedRange=await animated.evaluate(node=>node._fullLayout.xaxis.range);
  await page.getByRole('button',{name:'Hide Moving',exact:true}).click();
  await page.waitForFunction(()=>document.querySelector('.plotly-graph').data[0].visible==='legendonly');
  await page.evaluate(()=>{window.refresh();window.refresh();window.change('data');});
  await page.waitForFunction(()=>document.querySelector('.plotly-graph')._transitionData._frames[0]?.name==='replacement');
  assert(await animated.evaluate(node=>node===window.retained));assert.deepEqual(await animated.evaluate(node=>node._fullLayout.xaxis.range),retainedRange);
  assert.equal(await animated.evaluate(node=>node.data[0].visible),'legendonly');
  assert.equal(await animated.evaluate(node=>node._transitionData._frames.length),1);
  await page.evaluate(()=>window.change('invalid'));await page.getByRole('alert').waitFor();
  assert((await page.getByRole('alert').innerText()).includes('future_trace'));assert(await animated.evaluate(node=>node===window.retained));
  await page.evaluate(()=>window.change('repair'));await page.waitForFunction(()=>document.querySelectorAll('[role="alert"]').length===0);
  await page.getByRole('button',{name:'Show Moving',exact:true}).click();await page.waitForFunction(()=>document.querySelector('.plotly-graph').data[0].visible===true);
  assert.equal(await page.evaluate(()=>window.activeObservers),1);

  const three=await show('scatter3d');const camera=await three.evaluate(node=>JSON.parse(JSON.stringify(node._fullLayout.scene.camera)));
  await three.evaluate(node=>Plotly.relayout(node,{'scene.camera.eye':{x:3,y:1,z:1}}));
  await page.getByRole('button',{name:'Reset view',exact:true}).click();
  await page.waitForFunction(saved=>JSON.stringify(document.querySelector('.plotly-graph')._fullLayout.scene.camera)===JSON.stringify(saved),camera);

  for(const name of data.native_legacy_names) {
    const graph=await show(name);assert.equal(await graph.count(),1,await page.locator('#fixtures').innerText());
    const types=await graph.evaluate(node=>node._fullData.map(trace=>trace.type));assert.deepEqual(types,[name.slice(7)]);
    const pixels=await drawnPixels(graph,name);assert(pixels.magenta>10,`${name}: legacy trace data did not draw`);
    assert.equal(await page.locator('#fixtures [role="alert"]').count(),0,name);
    matrix.push({name,types,...pixels,renderer:'3.6.0',input_contract:'native dictionary reconstruction'});
  }

  for(const [name,message] of [['unsupported_trace','future_trace'],['blocked_image','not captured'],['invalid_image','could not load or decode'],['blocked_map','hosting policy']]) {
    await show(name);const alert=page.getByRole('alert');assert.equal(await alert.count(),1,name);
    const text=await alert.innerText();assert(text.includes(message),`${name}: ${text}`);assert(!text.includes('do-not-display'));
    assert.equal(await page.locator('.plotly-graph').count(),0,name);assert.equal(await page.evaluate(()=>window.activeObservers),0,name);
  }
  await show('surface');await page.evaluate(()=>window.remove());
  assert(await page.evaluate(()=>window.disposedGraphs.every(graph=>!graph._fullLayout&&!graph._ev&&!graph.querySelector('canvas'))));
  assert.equal(await page.evaluate(()=>window.activeObservers),0);
  // Dispose while a real WebGL chart is still being constructed too.
  await page.evaluate(()=>{window.show('volume');setTimeout(()=>window.remove(),0);});
  await page.waitForFunction(()=>document.querySelectorAll('.plotly-graph').length===0&&window.activeObservers===0);
  assert(await page.evaluate(()=>window.disposedGraphs.every(graph=>!graph._fullLayout)));
  violations.push(...await page.evaluate(()=>window.policyViolations || []));
  assert.deepEqual(errors,[]);assert.deepEqual(external,[]);assert.deepEqual(violations,[]);
  const summary={passed:true,browser:await browser.version(),renderer:'4.1.1',legacy_renderer:'3.6.0',registered_trace_families:registered.length,encoded_figures:data.figure_names.length,native_legacy_figures:data.native_legacy_names.length,matrix,errors,external_requests:external.length,csp_violations:violations.length,animation:true,retained_updates:true,scene_reset:true,cleanup:true,explicit_errors:4};
  await writeFile(join(output,'plotly-gap-browser.json'),JSON.stringify(summary,null,2)+'\n');console.log(JSON.stringify({...summary,matrix:matrix.map(({name,magenta,types})=>({name,magenta,types}))}));
}finally {await browser.close();await new Promise(resolve=>server.close(resolve));}
