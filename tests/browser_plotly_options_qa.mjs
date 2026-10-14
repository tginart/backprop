/**
 * Architecture: genuine SDK history -> release CSP -> existing pinned renderer
 * -> all family frame outcomes -> mixed subplot/camera/reset branches.
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
const fixture=spawnSync(python,['-B',join(root,'tests/plotly_gap_options_replay_fixture.py')],{encoding:'utf8',cwd:root,env:{...process.env,PYTHONPATH:join(root,'sdk')},maxBuffer:8*1024*1024});
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
window.updateCameras=()=>{const value=[...registry.values()][0];value.spec.layout.scene.camera.eye={x:2.1,y:1.1,z:1.7};value.spec.layout.scene2.camera.eye={x:-2.2,y:1.8,z:1.1};window.refresh();};window.ready=true;`;
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
const browser=await chromium.launch({headless:true,executablePath:process.env.CHROMIUM_EXECUTABLE,args:process.env.PLOTLY_OPTIONS_SOFTWARE?['--use-angle=swiftshader','--enable-unsafe-swiftshader']:['--enable-unsafe-swiftshader']});
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
  const png=await graph.screenshot({path:join(output,`plotly-options-${process.env.PLOTLY_OPTIONS_SOFTWARE?'software':'hardware'}-${name}.png`)});
  return page.evaluate(async encoded=> {
    const image=new Image();image.src='data:image/png;base64,'+encoded;await image.decode();
    const canvas=document.createElement('canvas');canvas.width=image.naturalWidth;canvas.height=image.naturalHeight;
    const context=canvas.getContext('2d');context.drawImage(image,0,0);const values=context.getImageData(0,0,canvas.width,canvas.height).data;
    let magenta=0;for(let i=0;i<values.length;i+=4)if(values[i]>45&&values[i+2]>45&&values[i]-values[i+1]>15&&values[i+2]-values[i+1]>15&&values[i+3]>0)magenta++;
    let cyan=0;for(let i=0;i<values.length;i+=4)if(values[i+1]>45&&values[i+2]>45&&values[i+1]-values[i]>15&&values[i+2]-values[i]>15&&values[i+3]>0)cyan++;return {width:canvas.width,height:canvas.height,magenta,cyan};
  },png.toString('base64'));
}

/** Decode only the established renderer's binary dtypes for value assertions.
 * Main callers: frame geometry comparisons. This test helper mirrors no viewer
 * rendering logic: it reads actual fullData and checks it against the genuine
 * SDK frame's base64 bytes, including the matrix shape sent by Plotly Python.
 */
function binaryValue(value) {
  if(!value || typeof value!=='object' || !value.bdata)return value;
  const constructors={i1:Int8Array,u1:Uint8Array,i2:Int16Array,u2:Uint16Array,i4:Int32Array,u4:Uint32Array,f4:Float32Array,f8:Float64Array};
  const bytes=Buffer.from(value.bdata,'base64'),array=Array.from(new constructors[value.dtype](bytes.buffer.slice(bytes.byteOffset,bytes.byteOffset+bytes.byteLength)));
  if(!value.shape)return array;
  const dimensions=value.shape.split(',').map(Number);assert.equal(dimensions.length,2);return Array.from({length:dimensions[0]},(_,index)=>array.slice(index*dimensions[1],(index+1)*dimensions[1]));
}

try {
  await page.goto(origin);await page.waitForFunction(()=>window.ready);
  const driver=await page.evaluate(()=>{const gl=document.createElement('canvas').getContext('webgl'),info=gl?.getExtension('WEBGL_debug_renderer_info');return info?gl.getParameter(info.UNMASKED_RENDERER_WEBGL):gl?.getParameter(gl.RENDERER);});
  const fields={scatter:'y',bar:'y',box:'y',violin:'y',funnel:'x',waterfall:'y',histogram:'x',histogram2d:'x',histogram2dcontour:'x',heatmap:'z',contour:'z',scatterpolar:'r',scatterpolargl:'r',barpolar:'r',scatterternary:'a',scattersmith:'imag',carpet:'y',scattercarpet:'a',contourcarpet:'z',quiver:'u',scattergl:'y',scatter3d:'z',surface:'z',mesh3d:'z',cone:'u',streamtube:'u',isosurface:'value',volume:'value',scattergeo:'lon',scattermap:'lon',densitymap:'lon',ohlc:'close',candlestick:'close'};
  for(const name of Object.keys(data.values).filter(name=>name.startsWith('frame_')&&!data.errors.includes(name))) {
    const type=name.slice(6),graph=await show(name);assert.equal(await graph.count(),1,await page.locator('#fixtures').innerText());
    const before=await drawnPixels(graph,name+'-base');assert(before.magenta>10,name+' initial data');
    await graph.evaluate(node=>{window.frameCompletion=new Promise(resolve=>node.once('plotly_animated',resolve));});
    await graph.getByText('Next',{exact:true}).click();
    await page.waitForFunction(type=>document.querySelector('.plotly-graph')._fullLayout.title.text==='Frame '+type,type);
    await page.evaluate(()=>window.frameCompletion);
    await graph.evaluate(node=>Promise.all(Object.values(node._fullLayout).map(value=>value?._subplot?.map).filter(Boolean).map(map=>new Promise(resolve=>{map.once('idle',resolve);map.triggerRepaint();}))));
    const after=await drawnPixels(graph,name+'-next');
    assert(after.cyan>10,`${name}: frame data did not draw (${JSON.stringify(after)})`);
    const field=fields[type],frame=data.values[name].spec.frames[1],index=frame.data.length-1;
    if(field) {
      const actual=await graph.evaluate((node,{index,field})=>{const normalize=value=>ArrayBuffer.isView(value)?Array.from(value):Array.isArray(value)?value.map(normalize):value;return normalize(node._fullData[index][field]);},{index,field});
      assert.deepEqual(actual,binaryValue(frame.data[index][field]),name+' frame coordinates');
    }
    if(type==='indicator')assert.equal(await graph.evaluate(node=>node._fullData[0].value),57);
    if(type==='table')assert((await graph.textContent()).includes('11'));
    if(['sunburst','treemap','icicle'].includes(type))assert.equal(await graph.evaluate(node=>node._fullData[0].level),'A');
    await graph.evaluate(node=>{window.frameCompletion=new Promise(resolve=>node.once('plotly_animated',resolve));});
    await graph.getByText('Back',{exact:true}).click();await page.waitForFunction(type=>document.querySelector('.plotly-graph')._fullLayout.title.text==='Base '+type,type);await page.evaluate(()=>window.frameCompletion);
    await graph.evaluate(node=>Promise.all(Object.values(node._fullLayout).map(value=>value?._subplot?.map).filter(Boolean).map(map=>new Promise(resolve=>{map.once('idle',resolve);map.triggerRepaint();}))));
    const back=await drawnPixels(graph,name+'-back');assert(back.magenta>10,name+' reverse control');
    assert.equal(await page.locator('#fixtures [role="alert"]').count(),0,name);
    let timed=null;
    if(['scatter','bar','sunburst','treemap','icicle','indicator','quiver'].includes(type)) {
      await graph.evaluate(node=>{window.timedStart=performance.now();window.frameCompletion=new Promise(resolve=>node.once('plotly_animated',()=>resolve(performance.now()-window.timedStart)));});
      await graph.getByText('Smooth',{exact:true}).click();const elapsed=await page.evaluate(()=>window.frameCompletion);
      assert(elapsed>=200,`${name}: duration was not honored (${elapsed})`);
      const final=await drawnPixels(graph,name+'-timed');assert(final.cyan>10,name+' timed frame did not draw');
      if(field)assert.deepEqual(await graph.evaluate((node,field)=>Array.from(node._fullData.at(-1)[field]),field),binaryValue(frame.data[index][field]),name+' timed coordinates');
      if(type==='indicator')assert.equal(await graph.evaluate(node=>node._fullData[0].value),57);
      if(['sunburst','treemap','icicle'].includes(type))assert.equal(await graph.evaluate(node=>node._fullData[0].level),'A');
      timed={elapsed_ms:elapsed,final_pixels:final,redraw:false,transition_duration:240,frame_duration:300};
    }
    matrix.push({name,types:await graph.evaluate(node=>node._fullData.map(trace=>trace.type)),module_animatable:await graph.evaluate(node=>Boolean(node._fullData.at(-1)._module.animatable)),before,after,back,geometry_field:field || null,timed});
  }
  const schema=await page.evaluate(()=>Object.entries(Plotly.PlotSchema.get().traces).map(([type,trace])=>({type,animated_keys:Object.entries(trace.attributes).filter(([,attribute])=>attribute?.anim).map(([key])=>key)})));
  assert.deepEqual(matrix.map(row=>row.name.slice(6)).sort(),schema.map(row=>row.type).sort());

  const inheritance=await show('animation_inheritance');await inheritance.getByText('Group',{exact:true}).click();
  await page.waitForFunction(()=>document.querySelector('.plotly-graph')._fullLayout.title.text==='Inherited frame');
  assert.deepEqual(await inheritance.evaluate(node=>Array.from(node._fullData[0].x)),['A','B']);assert.deepEqual(await inheritance.evaluate(node=>Array.from(node._fullData[0].y)),[5,1]);
  assert((await drawnPixels(inheritance,'inherited-group-frame')).cyan>10);
  const slider=inheritance.locator('.slider-rail-touch-rect'),sliderBounds=await slider.boundingBox();
  await slider.click({position:{x:2,y:8}});
  await page.waitForFunction(()=>document.querySelector('.plotly-graph')._fullData[0].y[0]===2);
  assert((await drawnPixels(inheritance,'slider-original')).magenta>10);
  await slider.click({position:{x:sliderBounds.width-2,y:8}});
  await page.waitForFunction(()=>document.querySelector('.plotly-graph')._fullData[0].y[0]===5);
  assert((await drawnPixels(inheritance,'slider-inherited')).cyan>10);

  const mixed=await show('mixed_scenes');assert.equal(await mixed.count(),1);
  const saved=await mixed.evaluate(node=>({scene:JSON.parse(JSON.stringify(node._fullLayout.scene.camera)),scene2:JSON.parse(JSON.stringify(node._fullLayout.scene2.camera)),polar:JSON.parse(JSON.stringify(node._fullLayout.polar.radialaxis.range))}));
  const mixedPixels=await drawnPixels(mixed,'mixed-initial');assert(mixedPixels.magenta>100);
  // Pixel quadrants must all contain saved data. A single successful scene
  // cannot hide an empty second scene, Cartesian subplot or polar subplot.
  const quadrants=await mixed.evaluate(async node=> {
    const image=new Image();image.src=await Plotly.toImage(node,{format:'png'});await image.decode();
    const canvas=document.createElement('canvas');canvas.width=image.naturalWidth;canvas.height=image.naturalHeight;const ctx=canvas.getContext('2d');ctx.drawImage(image,0,0);const pixels=ctx.getImageData(0,0,canvas.width,canvas.height).data,counts=[0,0,0,0];
    for(let y=0;y<canvas.height;y++)for(let x=0;x<canvas.width;x++){const i=(y*canvas.width+x)*4;if(pixels[i]>45&&pixels[i+2]>45&&pixels[i]-pixels[i+1]>15&&pixels[i+2]-pixels[i+1]>15)counts[(y<canvas.height*.62?0:2)+(x<canvas.width*.5?0:1)]++;}return counts;
  });assert(quadrants.every(count=>count>10),JSON.stringify(quadrants));
  await mixed.evaluate(node=>Plotly.relayout(node,{'scene.camera.eye':{x:3,y:1,z:.5},'scene2.camera.eye':{x:1,y:3,z:.5},'xaxis.range':[0,1],'yaxis.range':[2,3],'polar.radialaxis.range':[0,10],'polar.angularaxis.rotation':80}));
  await page.getByRole('button',{name:'Reset view',exact:true}).click();
  await page.waitForFunction(saved=>{const layout=document.querySelector('.plotly-graph')._fullLayout;return JSON.stringify(layout.scene.camera)===JSON.stringify(saved.scene)&&JSON.stringify(layout.scene2.camera)===JSON.stringify(saved.scene2);},saved);
  await page.waitForFunction(saved=>['scene','scene2'].every(key=>Math.abs(document.querySelector('.plotly-graph')._fullLayout[key]._scene.getCamera().eye.x-saved[key].eye.x)<1e-6),saved);
  assert.deepEqual(await mixed.evaluate(node=>node._fullLayout.xaxis.range),[-.5,1.5]);assert.deepEqual(await mixed.evaluate(node=>node._fullLayout.yaxis.range),[0,6]);assert.deepEqual(await mixed.evaluate(node=>node._fullLayout.polar.radialaxis.range),saved.polar);
  await page.evaluate(()=>window.updateCameras());await page.waitForFunction(()=>document.querySelector('.plotly-graph')._fullLayout.scene.camera.eye.x===2.1);
  await mixed.evaluate(node=>Plotly.relayout(node,{'scene.camera.eye':{x:4,y:4,z:4},'scene2.camera.eye':{x:4,y:4,z:4}}));
  await page.getByRole('button',{name:'Reset view',exact:true}).click();await page.waitForFunction(()=>document.querySelector('.plotly-graph')._fullLayout.scene.camera.eye.x===2.1&&document.querySelector('.plotly-graph')._fullLayout.scene2.camera.eye.x===-2.2);
  await page.waitForFunction(()=>Math.abs(document.querySelector('.plotly-graph')._fullLayout.scene._scene.getCamera().eye.x-2.1)<1e-6&&Math.abs(document.querySelector('.plotly-graph')._fullLayout.scene2._scene.getCamera().eye.x+2.2)<1e-6);
  // Export awaits each actual scene's redraw, so software-driver screenshots
  // cannot race the saved-camera metadata update and retain the previous pixels.
  await mixed.evaluate(node=>Plotly.toImage(node,{format:'png'}));
  await drawnPixels(mixed,'mixed-updated-reset');

  const responsive=await show('mixed_responsive');const originalWidth=await responsive.evaluate(node=>node.clientWidth);await page.setViewportSize({width:390,height:720});
  await page.waitForFunction(width=>document.querySelector('.plotly-graph').clientWidth<width,originalWidth);await page.waitForFunction(()=>document.querySelector('.plotly-graph')._fullLayout.width<=390);
  assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));await drawnPixels(responsive,'mixed-responsive-mobile');await page.setViewportSize({width:900,height:650});

  const options=await show('explicit_template_log');assert.equal(await options.evaluate(node=>node._fullLayout.paper_bgcolor),'#fff');assert.equal(await options.evaluate(node=>node._fullLayout.xaxis.type),'log');
  assert.equal(await options.locator('a').count(),0);assert((await page.locator('#fixtures').innerText()).includes('Links in chart text are shown as text'));
  await options.evaluate(node=>Plotly.relayout(node,{'xaxis.range':[.5,1.5],'yaxis.range':[3,5]}));await page.locator('.plotly-actions').getByRole('button',{name:'Reset axes',exact:true}).click();await page.waitForFunction(()=>document.querySelector('.plotly-graph')._fullLayout.xaxis.range[0]===0);
  assert.deepEqual(await options.evaluate(node=>node._fullLayout.xaxis.range),[0,2]);assert.deepEqual(await options.evaluate(node=>node._fullLayout.yaxis.range),[0,10]);await drawnPixels(options,'template-log-reset');

  for(const name of data.errors) {
    await show(name);const text=await page.getByRole('alert').innerText();assert(!text.includes('secret'));assert(text.includes(name.includes('type')?'future_trace':name.includes('mapping')?'outside the saved figure':name==='unsupported_timed_title'?'Enable frame redraw':'not captured'),name+': '+text);assert.equal(await page.locator('.plotly-graph').count(),0);
  }
  await show('mixed_scenes');await page.evaluate(()=>window.remove());assert(await page.evaluate(()=>window.disposedGraphs.every(node=>!node._fullLayout&&!node.querySelector('canvas'))));assert.equal(await page.evaluate(()=>window.activeObservers),0);
  violations.push(...await page.evaluate(()=>window.policyViolations || []));assert.deepEqual(errors,[]);assert.deepEqual(external,[]);assert.deepEqual(violations,[]);
  const summary={passed:true,browser:await browser.version(),driver,renderer:'4.1.1',registered_frame_families:matrix.length,schema,matrix,baseframe_and_group:true,slider_controls:true,mixed_quadrant_pixels:quadrants,mixed_camera_reset:true,updated_saved_cameras:true,cartesian_and_polar_reset:true,responsive_mobile:true,template_link_restrictions:true,explicit_errors:data.errors,cleanup:true,errors,external_requests:0,csp_violations:0};
  const mode=process.env.PLOTLY_OPTIONS_SOFTWARE?'software':'hardware';await writeFile(join(output,`plotly-options-${mode}.json`),JSON.stringify(summary,null,2)+'\n');console.log(JSON.stringify({...summary,matrix:matrix.map(row=>({name:row.name,frame_cyan:row.after.cyan}))}));
}finally {await browser.close();await new Promise(resolve=>server.close(resolve));}
