/**
 * Architecture: genuine saved SDK controls/history -> real pointer dispatch or
 * retained hydrate -> paired native event/RAF drawing -> exact settled geometry.
 * Main caller: plotly_animation_browser_qa's bounded options focus and full CI.
 * Why this exists: selection filtering and retained react transitions use two
 * native lifecycle paths. Their intermediate and final assertions stay distinct.
 */
import assert from 'node:assert/strict';

export const selectionCases=[
 {seed:null,label:'Forward',expected:['a','b','c']},
 {seed:null,label:'Reverse',expected:['c','b','a']},
 {seed:'b',label:'Resume',expected:['c']},
 {seed:'b',label:'Reverse resume',expected:['a']},
 ...[null,'a','c','d'].map(seed=>({seed,label:'Resume',expected:['a','b','c']})),
 ...['a','c'].map(seed=>({seed,label:'Reverse resume',expected:['c','b','a']})),
 {seed:'b',label:'All resume',expected:['c','d']},
 {seed:'a',label:'Explicit resume',expected:['b']},
 {seed:'b',label:'Inline resume',expected:[null,'c']},
 {seed:'b',label:'Inline reverse resume',expected:['a',null]},
 {seed:'a',label:'Array reverse',expected:['c','b','a'],durations:[110,160,110]},
 {seed:'b',label:'Array reverse',expected:['a'],durations:[110]},
 ...[
  {label:'Numeric group',expected:['1','2']},
  {label:'Numeric reverse',expected:['2','1']},
  {label:'Numeric names',expected:['2','1']},
  {label:'Defaults',expected:['1'],durations:[500]},
 ].map(item=>({seed:null,figure:'numeric_selection_animation',...item})),
];
const values={a:2,b:4,c:6,d:8,inline:3,'1':3,'2':5};

/** Observe actual points once native frame events have reached a painted state.
 * Main caller: runSelectionCases. Two RAF callbacks are used within each saved
 * finite frame, and final geometry uses the existing native drawing-work fence.
 * Read the actual SVG matrix, keeping raw attributes separately. Native redraw
 * can serialize225.4 while a tween serializes225.39999389648438; both resolve
 * to the identical browser SVG coordinate. No rounding or tolerance is applied.
 */
async function armSelection(page) {
 await page.evaluate(()=>{
  const nodes=[document.querySelector('#viewer .plotly-graph'),window.rawGraph];
  const shape=node=>{const matrix=node.querySelector('.scatterlayer .point').transform.baseVal.consolidate().matrix;return[matrix.e,matrix.f];};
  window.optionShape=shape;window.optionSelection=nodes.map(node=>({before:shape(node),before_attribute:node.querySelector('.scatterlayer .point').getAttribute('transform'),events:[],painted:[],dispatches:[],button_events:[]}));
  window.optionComplete=Promise.all(nodes.map((node,index)=>new Promise((resolve,reject)=>{
   const timer=setTimeout(()=>reject(Error('Native frame selection did not complete: '+JSON.stringify(window.optionSelection[index]))),12000);
   node.addEventListener('pointerdown',event=>{const button=event.target.closest('g.updatemenu-button');if(button)window.optionSelection[index].dispatches.push({label:button.textContent,current:node._fullLayout._currentFrame??null,time:performance.now(),pointer_type:event.pointerType});},{capture:true});
   node.on('plotly_buttonclicked',event=>window.optionSelection[index].button_events.push({label:event.button.label,current:node._fullLayout._currentFrame??null,time:performance.now()}));
   node.on('plotly_animatingframe',event=>{const record=window.optionSelection[index],entry={name:event.name,value:Array.from(event.frame.data[0].y),frame:event.animation.frame,transition:event.animation.transition,time:performance.now()};record.events.push(entry);requestAnimationFrame(()=>requestAnimationFrame(()=>{record.painted.push({name:entry.name,current:node._fullLayout?._currentFrame??null,value:node._fullData?Array.from(node._fullData[0].y):null,geometry:node._fullLayout?shape(node):null});}));});
   node.once('plotly_animated',()=>{clearTimeout(timer);resolve();});
  }))).then(async()=>{const result=await window.waitForPlotlyDrawing(nodes,shape);window.optionSelection.forEach((record,index)=>{record.after=result.geometry[index];record.after_attribute=nodes[index].querySelector('.scatterlayer .point').getAttribute('transform');record.current=nodes[index]._fullLayout._currentFrame;record.value=Array.from(nodes[index]._fullData[0].y);});return window.optionSelection;});
 });
}

/** Execute every finite selection guard through the saved native hit targets.
 * Main caller: browser QA. The independently rendered controls own the reference
 * queue, so assertions do not use a local reimplementation of frame selection.
 */
export async function runSelectionCases({page,show,nativeButton,pixels}) {
 const output=[];
 for(const [index,item] of selectionCases.entries()) {
  await page.evaluate(()=>window.remove());const graph=await show(item.figure || 'selection_animation',false),reference=page.locator('#reference > div');
  if(item.seed) {
   await page.evaluate(()=>{window.optionSeed=Promise.all([document.querySelector('#viewer .plotly-graph'),window.rawGraph].map(node=>new Promise(resolve=>node.once('plotly_animated',resolve))));});
   for(const node of [graph,reference])await nativeButton(node,'Seed '+item.seed).click();
   await page.evaluate(async()=>{await window.optionSeed;await window.waitForPlotlyDrawing([document.querySelector('#viewer .plotly-graph'),window.rawGraph],()=>null);});
   assert.deepEqual(await graph.evaluate(node=>({current:node._fullLayout._currentFrame,value:Array.from(node._fullData[0].y)})),{current:item.seed,value:[values[item.seed]]});
  }
  await armSelection(page);for(const node of [graph,reference])await nativeButton(node,item.label).click();
  const [viewer,native]=await page.evaluate(()=>window.optionComplete);
  for(const record of [viewer,native]) {
   assert.deepEqual(record.events.map(event=>event.name),item.expected,item.label+' native frame selection');
   assert.equal(record.dispatches.length,1);assert.equal(record.dispatches[0].current,item.seed);assert.equal(record.dispatches[0].label,item.label);assert.equal(record.dispatches[0].pointer_type,'mouse');assert.equal(record.button_events.length,1);assert.equal(record.button_events[0].label,item.label);
   assert.deepEqual(record.events.map(event=>event.value),item.expected.map(name=>[values[name??'inline']]));
   assert.deepEqual(record.painted.map(event=>event.name),item.expected,item.label+' each frame painted');
   for(const paint of record.painted){assert.equal(paint.current,paint.name);assert.deepEqual(paint.value,[values[paint.name??'inline']]);assert(paint.geometry?.every(Number.isFinite));}
   assert.equal(record.current,item.expected.at(-1));assert.deepEqual(record.value,[values[item.expected.at(-1)??'inline']]);
   if(item.durations)assert.deepEqual(record.events.map(event=>event.frame.duration),item.durations,'native reversed/filtered option reindex');
  }
  if(viewer.events.every(event=>event.transition.duration===0))assert.deepEqual(viewer.painted.map(event=>event.geometry),native.painted.map(event=>event.geometry),item.label+' exact drawn frames');
  else {
   // Omitted/empty options select native500ms interpolation. The two-RAF
   // observations are intermediate paints at independently dispatched times,
   // so require actual travel and retain them rather than call them final.
   // Exact final comparison still waits for the native drawing-work fence.
   for(const record of [viewer,native]){assert.equal(record.events.length,1);assert.equal(record.events[0].transition.duration,500);assert.notDeepEqual(record.painted[0].geometry,record.after);const initial=record.before[1],final=record.after[1],paint=record.painted[0].geometry[1];assert((paint-initial)/(final-initial)>=0&&(paint-initial)/(final-initial)<1,item.label+' visible native interpolation');}
  }
  assert.deepEqual(viewer.after,native.after,item.label+' exact final geometry');
  if(index===0) {
   await graph.evaluate(node=>window.optionRetained=node);await page.evaluate(()=>window.refresh());
   assert(await graph.evaluate(node=>node===window.optionRetained));assert.equal(await graph.evaluate(node=>node._fullLayout._currentFrame),'c');
   await graph.evaluate(async node=>{await window.rawRenderer.relayout(window.rawGraph,{'yaxis.range':[3,7]});await window.rawRenderer.relayout(node,{'yaxis.range':[3,7]});});
   await page.locator('#viewer .plotly-actions').getByRole('button',{name:'Reset axes',exact:true}).click();
   await page.evaluate(async()=>{await window.rawRenderer.relayout(window.rawGraph,{'yaxis.range':[0,10]});await window.waitForPlotlyDrawing([document.querySelector('#viewer .plotly-graph'),window.rawGraph],window.optionShape);});
   assert.deepEqual(await graph.evaluate(node=>node._fullLayout.yaxis.range),[0,10]);assert.equal(await graph.evaluate(node=>node._fullLayout._currentFrame),'c');
  }
  const visible=await pixels(graph,'selection-'+index);assert(visible.magenta.reduce((sum,value)=>sum+value,0)>10);output.push({...item,viewer,native,pixels:visible,retained_reset:index===0});
 }
 return output;
}

/** Compare the visible alert with the actual thrown JavaScript exception.
 * Main callers: runEmptyOptionCase and focused protocol-message unit tests.
 * Why this exists: browser automation can include the exception name in its
 * pageerror message. ErrorEvent.error or PromiseRejectionEvent.reason retains
 * the actual exception, also used by the viewer's catch. Preserve both without
 * guessing or stripping a prefix; reject every extra unhandled runtime error.
 */
export function assertEmptyOptionFailure(diagnostic,runtimeErrors) {
 assert.equal(diagnostic.native_exception?.name,'TypeError','native empty-array exception type retained');
 assert.equal(typeof diagnostic.native_exception.message,'string');
 assert(diagnostic.native_exception.message.length>0,'native JavaScript exception message retained');
 assert.equal(diagnostic.viewer_alert,'Could not animate chart: '+diagnostic.native_exception.message,'viewer reports the actual native engine exception');
 assert.equal(typeof diagnostic.native_runtime_error,'string');
 assert(diagnostic.native_runtime_error.length>0,'raw automation exception retained');
 assert.deepEqual(runtimeErrors,[diagnostic.native_runtime_error],'only the proven native option-array exception');
}

/** Prove native empty timing arrays fail and the viewer reports the same error.
 * Main caller: browser QA's option focus and full combined CI. A real pointer
 * drives each saved control on an independent fresh pair. Capture the alert,
 * native ErrorEvent, saved args, renderer and before/after state before checking
 * them, so any failed assertion leaves enough evidence to diagnose the branch.
 */
export async function runEmptyOptionCase({page,show,nativeButton,errors}) {
 await page.evaluate(()=>window.remove());
 const graph=await show('numeric_selection_animation',false),reference=page.locator('#reference > div'),offset=errors.length;
 const before=await page.evaluate(async()=>{
  const nodes=[document.querySelector('#viewer .plotly-graph'),window.rawGraph];await window.waitForPlotlyDrawing(nodes,()=>null);
  // Plotly creates the queue on first animate, rather than in newPlot/addFrames.
  // Record that fresh lifecycle state explicitly instead of calling it empty.
  const state=node=>({current:node._fullLayout._currentFrame??null,data_y:Array.from(node.data[0].y),full_data_y:Array.from(node._fullData[0].y),frames:node._transitionData._frames.map(frame=>frame.name),queue:node._transitionData._frameQueue===undefined?'not initialized':node._transitionData._frameQueue.length,transitioning:Boolean(node._transitioning),args:structuredClone(node.layout.updatemenus[0].buttons.find(button=>button.label==='Empty arrays').args)});
  window.emptyOptionState=state;
  const diagnostic={case:'empty animation timing arrays',renderer_version:window.rawRenderer.version,renderer_scripts:[...document.scripts].map(script=>script.src).filter(src=>src.includes('plotly')),before:nodes.map(state)};
  diagnostic.observation=nodes.map(node=>{const record={frames:[],pointers:[],button_events:[]};node.on('plotly_animatingframe',event=>record.frames.push(event.name));node.addEventListener('pointerdown',event=>record.pointers.push({label:event.target.closest('g.updatemenu-button')?.textContent,type:event.pointerType,current:node._fullLayout._currentFrame??null}),{capture:true});node.on('plotly_buttonclicked',event=>record.button_events.push({label:event.button.label,method:event.button.method,args:structuredClone(event.button.args),execute:event.button.execute,current:node._fullLayout._currentFrame??null}));return record;});
  window.emptyOptionDiagnostic=diagnostic;
  return diagnostic.before;
 });
 assert.deepEqual(before[0],before[1],'empty options start with the same fresh native state');
 assert.deepEqual(before[0],{current:null,data_y:[1],full_data_y:[1],frames:['1','2'],queue:'not initialized',transitioning:false,args:[['2'],{frame:[],transition:[]}]},'empty options use the actual saved fresh base and arguments');
 await nativeButton(graph,'Empty arrays').click();await page.waitForFunction(()=>document.querySelector('#viewer [role="alert"]'));
 await page.evaluate(()=>window.emptyOptionDiagnostic.viewer_alert=document.querySelector('#viewer [role="alert"]').textContent);
 assert.deepEqual(errors.slice(offset),[],'viewer catches the renderer exception');
 await page.evaluate(()=>{window.emptyOptionDiagnostic.native_exceptions=[];window.emptyOptionErrorListener=event=>{const error=event.type==='unhandledrejection'?event.reason:event.error;window.emptyOptionDiagnostic.native_exceptions.push({source:event.type,name:error?.name,message:error?.message,stack:error?.stack,constructor:error?.constructor?.name,event_message:event.message,filename:event.filename,line:event.lineno,column:event.colno});};for(const type of ['error','unhandledrejection'])window.addEventListener(type,window.emptyOptionErrorListener);});
 const caught=page.waitForEvent('pageerror',{timeout:12000});
 await nativeButton(reference,'Empty arrays').click();const error=await caught;
 const diagnostic=await page.evaluate(nativeError=>{for(const type of ['error','unhandledrejection'])window.removeEventListener(type,window.emptyOptionErrorListener);const diagnostic=window.emptyOptionDiagnostic;diagnostic.native_exception=diagnostic.native_exceptions[0];diagnostic.native_runtime_error=nativeError.message;diagnostic.automation_exception=nativeError;diagnostic.after=[document.querySelector('#viewer .plotly-graph'),window.rawGraph].map(window.emptyOptionState);return diagnostic;},{name:error.name,message:error.message,stack:error.stack});
 assert.equal(diagnostic.native_exceptions.length,1,'exactly one native JavaScript failure event');
 assertEmptyOptionFailure(diagnostic,errors.slice(offset));
 for(const record of diagnostic.observation){assert.deepEqual(record.frames,[]);assert.deepEqual(record.pointers,[{label:'Empty arrays',type:'mouse',current:null}]);assert.equal(record.button_events.length,1);assert.equal(record.button_events[0].label,'Empty arrays');assert.equal(record.button_events[0].method,'animate');assert.deepEqual(record.button_events[0].args,before[0].args);}
 await page.evaluate(()=>window.remove());assert(await page.evaluate(()=>window.disposed.every(node=>!node._fullLayout)));assert.equal(await page.evaluate(()=>window.activeObservers),0);
 return{...diagnostic,classification:'shared pinned renderer limitation',cleanup:true};
}

/** Compare one genuine same-key SDK history update with direct native react.
 * Main caller: runReactCases. Native transition events start each independent
 * clock; RAF observations include drawn point coordinates and axis tick labels.
 */
export async function runReactCases({page,show,pixels,serial=false,nativePair=false}) {
 const output=[];
 for(const ordering of ['layout first','traces first']) {
  const name='react_'+ordering.replaceAll(' ','_')+'_before';await page.evaluate(()=>window.remove());const graph=await show(name,false);if(nativePair)await page.evaluate(name=>window.useNativeLeft(name),name);
  await page.evaluate(async()=>{await window.waitForPlotlyDrawing([document.querySelector('#viewer .plotly-graph'),window.rawGraph],node=>node.querySelector('.scatterlayer .point').getAttribute('transform'));});
  const observing=page.evaluate(async()=>{
   const nodes=[document.querySelector('#viewer .plotly-graph'),window.rawGraph],duration=900;
   // Axis transitions transform ancestor groups and inversely scale symbols.
   // Compose all transforms before measuring the actual visible point center.
   const shape=node=>{const point=node.querySelector('.scatterlayer .point'),svg=node.querySelector('.main-svg').getBoundingClientRect(),center=new DOMPoint(0,0).matrixTransform(point.getScreenCTM()),bounds=point.getBoundingClientRect();return{point:[center.x-svg.x,center.y-svg.y],bounds:[bounds.x-svg.x,bounds.y-svg.y,bounds.width,bounds.height],ticks:[...node.querySelectorAll('.ytick')].map(tick=>({text:tick.textContent,transform:tick.querySelector('text')?.getAttribute('transform')}))};};
   const records=nodes.map(node=>({before:shape(node),samples:[],start:null,done:false,end_events:[],lifecycle:[]}));window.optionReact={records};window.optionRetained=nodes[0];
   nodes.forEach((node,index)=>{for(const event of ['plotly_relayout','plotly_afterplot','plotly_transitioninterrupted'])node.on(event,value=>records[index].lifecycle.push({event,time:performance.now()-records[index].start,value}));});
   nodes.forEach((node,index)=>{node.once('plotly_transitioning',()=>records[index].start=performance.now());node.on('plotly_transitioned',()=>{records[index].done=true;records[index].end_events.push({time:performance.now()-records[index].start,work:window.plotlyDrawingWork(node)});});});
   // Native transitioned can precede pending D3 paint. Continue observing both
   // charts until their actual drawing work finishes, then use exact geometry.
   window.optionReactArmed=true;await new Promise((resolve,reject)=>{const deadline=performance.now()+12000;const tick=()=>{nodes.forEach((node,index)=>{const record=records[index];if(record.start!==null)record.samples.push({time:(performance.now()-record.start)/duration,value:shape(node)});});if(records.every(record=>record.done)&&nodes.every(node=>{const work=window.plotlyDrawingWork(node);return !work.transitioning&&!work.queue&&!work.svgTransitions;}))resolve();else if(performance.now()>deadline)reject(Error('Retained/native react transition did not complete'));else requestAnimationFrame(tick);};requestAnimationFrame(tick);});
   const settled=await window.waitForPlotlyDrawing(nodes,shape);records.forEach((record,index)=>{record.after=settled.geometry[index];record.settled_elapsed=performance.now()-record.start;record.value=Array.from(nodes[index]._fullData[0].y);record.range=nodes[index]._fullLayout.yaxis.range;});return records;
  }).then(value=>({value}),error=>({error}));
  await page.waitForFunction(()=>window.optionReactArmed);await page.evaluate(value=>window.historyUpdate(value),{name,serial});
  const observed=await observing;if(observed.error)throw observed.error;const [viewer,native]=observed.value;
  assert(await graph.evaluate(node=>node===window.optionRetained),'SDK update retains the actual graph');
  assert.deepEqual(viewer.before,native.before);assert.deepEqual(viewer.after,native.after,ordering+' exact final point and ticks');
  for(const record of [viewer,native]){assert.deepEqual(record.value,[10]);assert.deepEqual(record.range,[0,20]);assert(record.settled_elapsed>=875,'saved native drawing transition duration');}
  const sampled=[.25,.5,.75].map(time=>{const nearest=record=>record.samples.reduce((best,sample)=>Math.abs(sample.time-time)<Math.abs(best.time-time)?sample:best);return{time,viewer:nearest(viewer),native:nearest(native)};});
  const span=native.after.point[1]-native.before.point[1],progress=sample=>(sample.value.point[1]-native.before.point[1])/span;assert(Math.abs(span)>50,'measurable native axis/data update');
  for(const sample of sampled)assert(Math.abs(progress(sample.viewer)-progress(sample.native))<.12,ordering+' paired intermediate point '+JSON.stringify(sample));
  const mid=sampled[1],nativeProgress=progress(mid.native);
  if(ordering==='traces first') {
   assert(nativeProgress>.25&&nativeProgress<.75,'native trace moves toward final geometry before axis update');
   assert.deepEqual(mid.native.value.ticks,native.before.ticks,'native axes wait while traces move');assert.deepEqual(mid.viewer.value.ticks,viewer.before.ticks);
  } else {
   assert(nativeProgress<-.05,'native old data moves with changing axes before final trace update');
   assert.notDeepEqual(mid.native.value.ticks,native.before.ticks,'native axes visibly interpolate first');assert.notDeepEqual(mid.viewer.value.ticks,viewer.before.ticks);
  }
  await graph.evaluate(node=>window.rawRenderer.relayout(node,{'yaxis.range':[0,5]}));if(nativePair)await graph.evaluate(node=>window.rawRenderer.relayout(node,{'yaxis.range':[0,20]}));else await page.locator('#viewer .plotly-actions').getByRole('button',{name:'Reset axes',exact:true}).click();await page.waitForFunction(()=>document.querySelector('#viewer .plotly-graph')._fullLayout.yaxis.range[1]===20);
  const visible=await pixels(graph,name+'-updated');assert(visible.magenta.reduce((sum,value)=>sum+value,0)>10);output.push({name,ordering,viewer,native,sampled,pixels:visible,retained:true,reset_latest_saved_range:true});
  await page.evaluate(()=>window.optionReactArmed=false);
 }
 return output;
}
