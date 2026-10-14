/**
 * Architecture: genuine saved frame -> real native control -> RAF observations
 * of drawn SVG/style/data -> independent renderer comparison -> retained update.
 * Main caller: plotly_animation_browser_qa's finite coverage focus and full CI.
 * Why this exists: a final fullData value cannot prove that a marker, line,
 * implicit coordinate, point identity or replacement frame was actually drawn.
 * Layout transitions are observed serially so the documented shared native
 * concurrent-transition defect cannot masquerade as a viewer difference.
 */
import assert from 'node:assert/strict';

/** Capture only observable chart data and actual colored SVG paths.
 * Main callers: armBranch and retained-update comparisons, inside the browser.
 * Coordinates include parent transforms and are relative to the chart, so
 * side-by-side native and viewer placement does not change the measurements.
 */
export function branchShape(node) {
 const normal=value=>ArrayBuffer.isView(value)?Array.from(value):Array.isArray(value)?value.map(normal):value&&typeof value==='object'?Object.fromEntries(Object.entries(value).map(([key,item])=>[key,normal(item)])):value;
 const color=value=>[...value.matchAll(/[\d.]+/g)].map(match=>Number(match[0]));
 const paths=[...node.querySelectorAll('.main-svg path')].filter(path=>{
  const style=getComputedStyle(path);return [style.fill,style.stroke].some(value=>value.includes('232, 63, 232')||value.includes('0, 232, 232')) || path.closest('.scatterlayer,.barlayer,.quiverlayer,.carpetlayer,.shapelayer,.selectionlayer');
 }).map(path=>{const bounds=path.getBBox(),matrix=path.getCTM(),corners=[[bounds.x,bounds.y],[bounds.x+bounds.width,bounds.y+bounds.height]].map(([x,y])=>new DOMPoint(x,y).matrixTransform(matrix)),style=getComputedStyle(path);return{id:path.__data__?.id??null,
  box:[corners[0].x,corners[0].y,corners[1].x-corners[0].x,corners[1].y-corners[0].y],path:path.getAttribute('d'),transform:path.getAttribute('transform'),
  fill:color(style.fill),stroke:color(style.stroke),opacity:Number(style.opacity),fill_opacity:Number(style.fillOpacity),stroke_width:parseFloat(style.strokeWidth)};});
  const fields=['type','uid','ids','x','y','x0','dx','y0','dy','u','v','r','theta','fillcolor'];
 const data=node._fullData.map(trace=>{const item=Object.fromEntries(fields.filter(field=>field!=='uid'||['first','second'].includes(trace.uid)).filter(field=>trace[field]!==undefined).map(field=>[field,normal(trace[field])]));if(trace.line)item.line={color:trace.line.color,width:trace.line.width};if(trace.marker)item.marker={color:normal(trace.marker.color),size:normal(trace.marker.size),opacity:normal(trace.marker.opacity),standoff:normal(trace.marker.standoff),line:{color:normal(trace.marker.line?.color),width:normal(trace.marker.line?.width)}};return item;});
 const layoutItems=items=>(items || []).map(item=>({x0:item.x0,x1:item.x1,y0:item.y0,y1:item.y1,line:{color:item.line?.color,width:item.line?.width}}));
 return{paths,data,ranges:{x:normal(node._fullLayout.xaxis?.range),y:normal(node._fullLayout.yaxis?.range),polar:normal(node._fullLayout.polar?.radialaxis.range)},
  layout:{shapes:layoutItems(node._fullLayout.shapes),selections:layoutItems(node._fullLayout.selections)},current:node._fullLayout._currentFrame??null};
}

/** Observe one chart without sharing another active native transition clock.
 * Main caller: runAttributeCases. Lifecycle events record forwarded timings;
 * RAF samples measure rendered paths while fullData may already be final.
 * Error/rejection listeners only retain diagnostic events during this phase;
 * they do not catch or suppress native failures. The viewer's actual inline
 * alert is captured separately so a missing runtime error cannot imply success.
 */
async function armBranch(page,index) {
 await page.evaluate(index=>{
  const node=[document.querySelector('#viewer .plotly-graph'),window.rawGraph][index],shape=window.branchShape;
  const record={side:index?'native':'viewer',renderer_version:window.rawRenderer.version,before:shape(node),samples:[],start:null,done:false,events:[],control_events:[],runtime_exceptions:[],inline_alerts:[]};window.branchObservation=record;
  node.once('plotly_buttonclicked',event=>record.control_events.push({label:event.button.label,method:event.button.method,args:structuredClone(event.button.args),execute:event.button.execute,current:node._fullLayout._currentFrame??null,time:performance.now()}));
  const captureException=event=>{const error=event.type==='unhandledrejection'?event.reason:event.error;record.runtime_exceptions.push({source:event.type,name:error?.name,message:error?.message,stack:error?.stack,constructor:error?.constructor?.name,event_message:event.message,filename:event.filename,line:event.lineno,column:event.colno,phase:record.done?'after frame queue completion':record.start===null?'before first saved frame':'during saved frame',elapsed:record.start===null?null:performance.now()-record.start});};
  for(const type of ['error','unhandledrejection'])window.addEventListener(type,captureException);
  const captureAlerts=()=>{if(index)return[];const container=node.closest('[data-plotly-id]');return[...container.querySelectorAll('[role="alert"]')].map(alert=>{const style=getComputedStyle(alert),box=alert.getBoundingClientRect();return{text:alert.innerText,visible:box.width>0&&box.height>0&&style.display!=='none'&&style.visibility!=='hidden'&&Number(style.opacity)>0,display:style.display,visibility:style.visibility,opacity:style.opacity,box:{x:box.x,y:box.y,width:box.width,height:box.height}};});};
  window.branchFinished=new Promise((resolve,reject)=>{
   const deadline=performance.now()+12000;
   node.once('plotly_animatingframe',event=>{record.start=performance.now();record.events.push({name:event.name,frame:event.animation.frame,transition:event.animation.transition});});
   node.once('plotly_animated',()=>{record.done=true;record.elapsed=performance.now()-record.start;});
   const tick=()=>{if(record.start!==null&&!record.done)record.samples.push({time:(performance.now()-record.start)/900,value:shape(node)});
    if(record.done)window.waitForPlotlyDrawing([node],shape).then(result=>{record.after=result.geometry[0];record.inline_alerts=captureAlerts();resolve(record);},reject);
    else if(performance.now()>deadline)reject(Error('Concrete animation branch did not complete'));else requestAnimationFrame(tick);};requestAnimationFrame(tick);
  }).finally(()=>{record.inline_alerts=captureAlerts();for(const type of ['error','unhandledrejection'])window.removeEventListener(type,captureException);record.exception_observers_disconnected=true;});
 },index);
}

const numbers=value=>value.paths.flatMap(path=>[...path.box,...path.fill,...path.stroke,path.opacity,path.fill_opacity,path.stroke_width]);
const nearest=(record,time)=>record.samples.reduce((best,sample)=>Math.abs(sample.time-time)<Math.abs(best.time-time)?sample:best);
const nativeLayoutNegatives=new Set(['branch_layout_ranges_styles','branch_layout_ranges','branch_layout_styles']);

/** Read the native synchronous exception stack, preserving async ancestry.
 * Main caller: assertNativeLayoutExceptions. Firefox explicitly separates its
 * appended Promise ancestry with "promise callback*". Viewer and native control
 * dispatchers have different ancestry; their actual throwing renderer frames
 * must still match byte for byte. Other engines keep their entire raw stack.
 */
function primaryRendererStack(stack) {
 assert.equal(typeof stack,'string','actual JavaScript exception stack retained');
 const lines=stack.split('\n'),boundary=lines.findIndex(line=>line.startsWith('promise callback*'));
 const primary=boundary<0?stack:lines.slice(0,boundary).join('\n');
 assert(primary.includes('/plotly-strict-4.1.1.min.js:'),'primary exception stack identifies the pinned renderer');
 return primary;
}

/** Match only the three proven native layout failures using actual JS events.
 * Main caller: runAttributeCases after exact stale geometry/range assertions.
 * Why this exists: WebKit can omit a repeated automation pageerror while both
 * native unhandledrejection events are present. Require exactly one real event
 * per side, exact throwing-stack equality, cleanup and no inline alerts. Raw
 * transport remains separate, with zero or one exact source-derived message
 * per phase. Repeated transport can be absent from both sides, so actual JS
 * events are the mandatory evidence. No global allowlist is produced; the
 * browser still compares its complete ordered error list exactly.
 */
function nativeLayoutException(name,record) {
 assert(nativeLayoutNegatives.has(name),'only the three named native layout negatives use source-event matching');
  assert.equal(record.renderer_version,'4.1.1','negative phase uses the pinned renderer');
  assert.equal(record.exception_observers_disconnected,true,'active exception observers cleaned up');
  assert.deepEqual(record.inline_alerts,[],'native layout negative has no unexpected inline alert');
  assert(Array.isArray(record.runtime_exceptions),'actual JavaScript event record retained');
  assert.equal(record.runtime_exceptions.length,1,'exactly one actual JavaScript exception per side');
  const exception=record.runtime_exceptions[0];
  assert(['error','unhandledrejection'].includes(exception.source),'native exception has a standard browser source');
  assert.equal(exception.name,'TypeError','native layout exception type');assert.equal(exception.constructor,'TypeError','native exception constructor');
  assert.equal(typeof exception.message,'string');assert(exception.message.length>0,'actual native exception message retained');
  assert.equal(exception.phase,'during saved frame','exception belongs to the active saved frame');
  assert(Number.isFinite(exception.elapsed)&&exception.elapsed>=0,'finite active exception observation time');
  assert(Array.isArray(record.runtime_errors),'raw automation phase record retained');
  assert(record.runtime_errors.length<=1,'at most one automation error per actual native exception');
  for(const message of record.runtime_errors)assert(message===exception.message||message===exception.name+': '+exception.message,'automation error matches the exact actual JavaScript exception');
  return{source:exception.source,name:exception.name,message:exception.message,stack:primaryRendererStack(exception.stack)};
}

export function assertNativeLayoutExceptions(name,viewer,native) {
 const actual=[viewer,native].map(record=>nativeLayoutException(name,record));
 assert.deepEqual(actual[0],actual[1],'same actual native renderer exception per side');
 return{classification:'known native layout exception; rendering remains partial',actual_exception:actual[0],transport_messages:{native:native.runtime_errors,viewer:viewer.runtime_errors}};
}

/** The source defect remains mandatory evidence; a repaired viewer stays clean.
 * Main caller: the three finite layout branches below. Historical paired-error
 * regression records still use assertNativeLayoutExceptions unchanged. The
 * repaired output is independently checked by ordinary static native drawing,
 * so a stale viewer cannot pass merely because the source also failed.
 */
export function assertRepairedNativeLayoutExceptions(name,viewer,native) {
 const actual=nativeLayoutException(name,native);
 assertCleanBranchEvents(viewer,name+' repaired viewer');
 assert.deepEqual(viewer.runtime_errors,[],name+' repaired viewer has no automation error');
 return{classification:'native source layout exception; viewer selection-stage repair',actual_exception:actual,transport_messages:{native:native.runtime_errors,viewer:viewer.runtime_errors}};
}

/** Render each complete finite target through an independent ordinary newPlot.
 * Main caller: runAttributeCases for the three layout fixtures. These existing
 * fixtures contain complete shape/selection arrays, so their replacement here
 * does not reuse the production selection-container merge implementation.
 */
async function staticLayoutTarget(page,source,target) {
 return page.evaluate(async({source,target})=>{
  const merge=(base,value)=>{if(!value||typeof value!=='object'||Array.isArray(value))return structuredClone(value);const result=structuredClone(base||{});for(const [key,item] of Object.entries(value))result[key]=merge(result[key],item);return result;};
  const data=structuredClone(source.data);for(const [index,trace] of target.data.entries()){const parent=target.traces?.[index]??index;data[parent]=merge(data[parent],trace);}
  const node=document.createElement('div');document.querySelector('#reference').append(node);
  try{await window.rawRenderer.newPlot(node,data,merge(source.layout,target.layout),{responsive:false,displaylogo:false,displayModeBar:false});await window.waitForPlotlyDrawing([node],window.branchShape);return window.branchShape(node);}
  finally{window.rawRenderer.purge(node);node.remove();}
 },{source,target});
}

/** Positive branches require clean actual events as well as clean transport.
 * Main callers: attribute positives and repeated retained-frame updates. The
 * native source observer catches errors even if automation omits a duplicate.
 */
function assertCleanBranchEvents(record,name) {
 assert.deepEqual(record.runtime_exceptions,[],name+' no actual JavaScript rendering exception');
 assert.deepEqual(record.inline_alerts,[],name+' no unexpected inline rendering alert');
 assert.equal(record.exception_observers_disconnected,true,name+' exception observers cleaned up');
}

/** Compare every concrete nested/coordinate/identity branch against native SVG.
 * Main caller: browser QA. Exact settled paths/styles and source target fields
 * are required even when the native renderer updates a branch instantaneously.
 */
export async function runAttributeCases({page,show,nativeButton,pixels,names,errors}) {
 await page.evaluate(`window.branchShape=(${branchShape.toString()})`);
 const output=[];
 for(const name of names) {
  await page.evaluate(()=>window.remove());const redraw=/layout_|carpet_/.test(name),graph=await show(name,redraw),reference=page.locator('#reference > div'),records=[];
  const layoutBranch=nativeLayoutNegatives.has(name),source=layoutBranch?await page.evaluate(()=>({data:structuredClone(window.rawGraph.data),layout:structuredClone(window.rawGraph.layout)})):null;
  // Native-first serial observation preserves the independent reference and
  // isolates the previously documented shared D3 layout-transition boundary.
  for(const index of [1,0]){const offset=errors.length;await armBranch(page,index);await nativeButton(index?reference:graph,'Animate').click();records[index]=await page.evaluate(()=>window.branchFinished);records[index].runtime_errors=errors.slice(offset);}
  const [viewer,native]=records;
  // Retain both complete active phases before any comparison can fail. This
  // includes the actual error source and alert, not just transport wording.
  const target=await graph.evaluate(node=>node._transitionData._frames.find(frame=>frame.name==='next'));
  await page.evaluate(value=>window.branchPair=value,{name,viewer,native,target});
  assert.deepEqual(viewer.before,native.before,name+' initial drawn/data state');if(!layoutBranch)assert.deepEqual(viewer.after,native.after,name+' exact settled drawn/data state');
  for(const record of records){assert.equal(record.events.length,1);assert.equal(record.events[0].name,'next');const frameDuration=name.startsWith('branch_layout_')?1100:900;assert.equal(record.events[0].frame.duration,frameDuration);assert.equal(record.events[0].transition.duration,900);assert(record.elapsed>=frameDuration-25,name+' saved frame duration');assert(record.samples.length>5,name+' actual RAF observations');}
  const before=numbers(native.before),after=numbers(native.after),sampled=[];
  for(const time of layoutBranch?[]:[.25,.5,.75]){
   const actual=nearest(viewer,time),expected=nearest(native,time),a=numbers(actual.value),b=numbers(expected.value);
   assert.equal(a.length,b.length,name+' native intermediate path shape');
   const delta=a.map((value,index)=>Math.abs(value-b[index])/Math.max(1,Math.abs((after[index]??value)-(before[index]??value))));
   assert(Math.max(...delta)<.12,name+' actual intermediate paths/styles '+JSON.stringify({time,max_delta:Math.max(...delta)}));
   sampled.push({time,viewer:actual,native:expected,max_normalized_delta:Math.max(...delta)});
  }
  const missingRanges=['xaxis','yaxis'].some((axis,index)=>target.layout?.[axis]?.range&&JSON.stringify(native.after.ranges[index?'y':'x'])!==JSON.stringify(target.layout[axis].range));
  const changed=JSON.stringify(viewer.before.paths)!==JSON.stringify(viewer.after.paths);
  const nativeLimitation=layoutBranch&&(missingRanges||JSON.stringify(native.before.paths)===JSON.stringify(native.after.paths));
  let exceptionMatch,staticTarget;
  if(nativeLimitation) {
   // These are explicit defect observations, not successful frame-layout
   // assertions. Preserve the target alongside exact matched native failures.
   // A future engine fixing native drawing must meet the positive assertions.
   assert.deepEqual(native.after.paths,native.before.paths,name+' independent native stale SVG paths');
   for(const [axis,key] of [['xaxis','x'],['yaxis','y']])if(target.layout?.[axis]?.range)assert.deepEqual(native.after.ranges[key],native.before.ranges[key],name+' shared native unchanged axis range');
   exceptionMatch=assertRepairedNativeLayoutExceptions(name,viewer,native);
   staticTarget=await staticLayoutTarget(page,source,target);
   const geometry=record=>{const shape=structuredClone(record);delete shape.current;return shape;};
   await page.evaluate(value=>window.branchPair.static_target=value,staticTarget);
   assert.deepEqual(geometry(viewer.after),geometry(staticTarget),name+' repaired exact independent static target');
   assert(changed,name+' repaired visible path/style change');
  }
  else for(const record of records){assertCleanBranchEvents(record,name);assert.deepEqual(record.runtime_errors,[],name+' no active rendering exception');}
  for(const [index,trace] of target.data.entries()){
   const parent=target.traces?.[index]??index,actual=viewer.after.data[parent];
   for(const [key,value] of Object.entries(trace))if(!['type','marker','line'].includes(key)) {
    // Native coercion omits implicit origins/steps when template x/y arrays
    // provide the actual quiver coordinates. Exact native fullData and SVG
    // equality above checks this branch without requiring an inactive field.
    if(actual.type==='quiver'&&['x0','dx','y0','dy'].includes(key)&&actual[key]===undefined&&actual.x!=null&&actual.y!=null)continue;
    assert.deepEqual(actual[key],value,name+' saved '+key);
   }
   if(trace.line)for(const [key,value] of Object.entries(trace.line))if(['color','width'].includes(key))assert.deepEqual(actual.line[key],value,name+' saved line.'+key);
   if(trace.marker)for(const [key,value] of Object.entries(trace.marker))if(['color','size','opacity','standoff'].includes(key))assert.deepEqual(actual.marker[key],value,name+' saved marker.'+key);
   if(trace.marker?.line)for(const [key,value] of Object.entries(trace.marker.line))assert.deepEqual(actual.marker.line[key],value,name+' saved marker.line.'+key);
  }
  for(const [axis,key] of [['xaxis','x'],['yaxis','y']])if(target.layout?.[axis]?.range)assert.deepEqual(viewer.after.ranges[key],target.layout[axis].range,name+' saved '+axis+'.range');
  if(target.layout?.polar?.radialaxis?.range)assert.deepEqual(viewer.after.ranges.polar,target.layout.polar.radialaxis.range,name+' saved polar range');
  for(const family of ['shapes','selections'])for(const [index,item] of (target.layout?.[family] || []).entries())for(const [key,value] of Object.entries(item))if(['x0','x1','y0','y1','line'].includes(key))assert.deepEqual(viewer.after.layout[family][index][key],value,name+' saved '+family+'.'+key);
  const visible=await pixels(graph,name);assert(visible.magenta.reduce((sum,value)=>sum+value,0)+visible.cyan.reduce((sum,value)=>sum+value,0)>10,name+' actual colored paths');
  if(name==='branch_scatter_ids')assert.deepEqual(viewer.after.data[0].ids,['c','a','b']);
  if(name==='branch_remapped_traces')assert.deepEqual(viewer.after.data.map(trace=>trace.y),[[6],[8]]);
  if(!name.endsWith('carpet_ids')&&!nativeLimitation)assert(changed,name+' visible path/style change');
  output.push({name,redraw,viewer,native,sampled,visible_change:changed,pixels:visible,
   ...(nativeLimitation?{native_limitation:'Pinned native animation retains old SVG/ranges and rejects; viewer repairs its selection prepare/draw stage. Intermediate easing remains partial.',viewer_layout_repaired:true,static_target:staticTarget,saved_target:target,native_exception_match:exceptionMatch}:{})});
  console.log('Verified attribute '+name+(nativeLimitation?' (native defect retained; viewer layout repaired)':''));
 }
 return output;
}

/** Replace saved frames repeatedly on the same chart after real controls.
 * Main caller: browser QA. Each descriptor is a separately logged SDK history
 * row; native react/frame replacement is the independent reference lifecycle.
 */
export async function runRetainedFrameCases({page,show,nativeButton,pixels}) {
 await page.evaluate(`window.branchShape=(${branchShape.toString()})`);
 const name='retained_frames_before',graph=await show(name,false),reference=page.locator('#reference > div'),output=[];
 await graph.evaluate(node=>window.branchRetained=node);
 for(const index of [0,1,2,3]) {
  await page.evaluate(async({name,index})=>window.replaceSavedHistory({name,index}),{name,index});
  await page.waitForFunction(()=>{const node=document.querySelector('#viewer .plotly-graph');return node===window.branchRetained&&JSON.stringify(node._fullData.map(trace=>[trace.uid,Array.from(trace.y)]))===JSON.stringify(window.rawGraph._fullData.map(trace=>[trace.uid,Array.from(trace.y)]));});
  await page.evaluate(async()=>window.waitForPlotlyDrawing([document.querySelector('#viewer .plotly-graph'),window.rawGraph],window.branchShape));
  const frames=await graph.evaluate(node=>node._transitionData._frames.map(frame=>frame.name));assert.deepEqual(frames,[index%2?'replacement':'next']);
  const observations=[];
  for(const position of [1,0]){await armBranch(page,position);await nativeButton(position?reference:graph,'Animate').click();observations[position]=await page.evaluate(()=>window.branchFinished);}
  for(const record of observations)assertCleanBranchEvents(record,'retained repeated update '+index);
  assert.deepEqual(observations[0].after,observations[1].after,'retained repeated update '+index+' exact drawn/data state');
  assert.equal(await graph.evaluate(node=>node===window.branchRetained),true);assert.equal(await graph.evaluate(node=>node._fullLayout._currentFrame),frames[0]);
  assert.deepEqual(await graph.evaluate(node=>node._fullData.map(trace=>trace.uid)),index%2?['second','first']:['first','second']);
  const visible=await pixels(graph,'retained-'+index);assert(visible.magenta.reduce((sum,value)=>sum+value,0)>10);assert(visible.cyan.reduce((sum,value)=>sum+value,0)>10);
  output.push({index,frames,same_node:true,viewer:observations[0],native:observations[1],pixels:visible});
 }
 return output;
}
