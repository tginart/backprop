/**
 * Architecture: logged JSON -> safe resource boundary -> pinned local Plotly ->
 * persistent chart DOM. Plotly owns trace geometry, hover and figure interactions;
 * Backprop owns loading, errors, keyboard controls, lifecycle and downloads.
 * Main callers: app rich values and media-view replacement.
 * Why this exists: translating a subset of Plotly silently changes charts.
 * The original specification stays available; provider access follows app policy.
 */
import {resolvePlotlyResources,usesLegacyMap,animationFrames} from './plotly-resources.mjs';
import {plotlyResetPatch} from './plotly-reset.mjs';
export const PLOTLY_SOURCE = './plotly-strict-4.1.1.min.js';
const own = (object, key) => Object.prototype.hasOwnProperty.call(object, key);
const selectionKey=key=>key==='selections'||key.startsWith('selections[')||key.startsWith('selections.');
const copy=value=>value===undefined?undefined:JSON.parse(JSON.stringify(value));

/** Merge the selection container using pinned native frame inheritance rules.
 * Main caller: bindPlotlyFrameSelections, after native baseframe resolution.
 * Why this exists: frame arrays merge their items by index; ordinary relayout
 * replaces the entire array. An omitted field retains its previous value, a
 * null field removes it, and an empty frame array does not erase older items.
 * Flattened native layout paths are expanded before applying those same rules.
 */
export function frameSelections(previous,layout={}) {
  const patch={};
  for(const [key,value] of Object.entries(layout))if(selectionKey(key)) {
    const path=key.replace(/\[(\d+)\]/g,'.$1').split('.');let current=patch;
    for(let index=0;index<path.length-1;index++)current=current[path[index]]??= /^\d+$/.test(path[index+1])?[]:{};
    current[path.at(-1)]=copy(value);
  }
  if(!own(patch,'selections'))return copy(previous);
  if(!Array.isArray(patch.selections))return copy(patch.selections);
  const merge=(base,value)=>{
    if(!value||typeof value!=='object'||Array.isArray(value))return copy(value);
    const result=base&&typeof base==='object'&&!Array.isArray(base)?copy(base):{};
    for(const [key,item] of Object.entries(value))result[key]=merge(result[key],item);
    return result;
  };
  const result=Array.isArray(previous)?copy(previous):[];
  for(let index=0;index<patch.selections.length;index++)if(own(patch.selections,index))result[index]=merge(result[index],patch.selections[index]);
  return result;
}

/** Stage persistent selections across the pinned renderer's calc/draw boundary.
 * Main caller: mount, once per chart; retained updates keep the same listeners.
 * Why this exists: 4.1.1 recalculates traces, then reselects before their SVG
 * nodes exist. That rejects the transition and leaves axes/shapes/traces stale.
 * Native animatingframe runs before that asynchronous prepare stage. Temporarily
 * omit selections there, restore saved inputs at transitioning, and let the
 * native final redraw apply them after trace nodes exist. Native timing, frame
 * selection, queue and controls are unchanged. No error or renderer is wrapped.
 * Only browser copies are staged. Overlapping prepares keep selections omitted
 * until each reaches drawing; named template items must be staged as well.
 * Caught failures, retained updates and disposal explicitly restore any pending
 * inputs. Interruption/completion signals cannot prematurely restore a prepare.
 */
export function bindPlotlyFrameSelections(graph,Plotly) {
  if(Plotly.version!=='4.1.1')return()=>{};
  const pending=[],templates=new Map();let desired,templateDesired;
  const restorePatch=({layout,patch,templatePatch})=>{
    for(const key of Object.keys(layout))if(selectionKey(key))delete layout[key];
    Object.assign(layout,patch);
    if(templatePatch){if(templatePatch.value===undefined)delete templatePatch.input.selections;else templatePatch.input.selections=templatePatch.value;}
  };
  const restoreInputs=()=>{
    if(desired===undefined)delete graph.layout.selections;else graph.layout.selections=copy(desired);
    for(const [input,value] of templates){if(value===undefined)delete input.selections;else input.selections=value;}templates.clear();
    const input=graph.layout.template?.layout;if(input){if(templateDesired===undefined)delete input.selections;else input.selections=copy(templateDesired);}
  };
  const drawn=()=>{if(!pending.length)return;restorePatch(pending.shift());if(!pending.length)restoreInputs();};
  const stage=event=>{
    const layout=event.frame.layout || {},patch=Object.fromEntries(Object.entries(layout).filter(([key])=>selectionKey(key)));
    const templateInput=graph.layout.template?.layout,templateFrame=layout.template?.layout;
    if(!(pending.length||graph.layout.selections?.length||graph._fullLayout.selections?.length||templateInput?.selections?.length||templateFrame?.selections?.length||Object.keys(patch).length))return;
    if(!pending.length){desired=copy(graph.layout.selections);templateDesired=copy(templateInput?.selections);}
    desired=frameSelections(desired,layout);
    if(own(layout,'template')&&layout.template==null)templateDesired=undefined;
    else if(templateFrame&&own(templateFrame,'selections'))templateDesired=copy(templateFrame.selections);
    const templatePatch=templateFrame&&own(templateFrame,'selections')?{input:templateFrame,value:templateFrame.selections}:null;
    pending.push({layout,patch,templatePatch});
    for(const key of Object.keys(patch))delete layout[key];
    layout.selections=[];graph.layout.selections=[];
    if(templateInput){if(!templates.has(templateInput))templates.set(templateInput,copy(templateInput.selections));templateInput.selections=[];}if(templatePatch)templateFrame.selections=[];
  };
  graph.on('plotly_animatingframe',stage);graph.on('plotly_transitioning',drawn);
  // A zero-duration queue can announce interruption/completion while another
  // prepare is still pending. Restore only after all observed prepares reach
  // transitioning; early restoration would reintroduce the native exception.
  const restore=()=>{const count=pending.length;if(count){for(const item of pending)restorePatch(item);pending.length=0;restoreInputs();}return count;};
  const dispose=()=>{restore();graph.removeListener('plotly_animatingframe',stage);graph.removeListener('plotly_transitioning',drawn);};
  // Caught command failures and retained react updates end their old input
  // lifecycle without removing the reusable chart's event listeners.
  dispose.restore=restore;
  /** Stage a complete retained figure using replacement, not frame merge.
   * Main caller: mount's native react stage after old animation drains. Timed
   * react shares the same premature reselect branch, but omitted selections
   * in a new figure must clear old inputs. Its final redraw restores rendering
   * even when native react did not need to emit a transitioning event.
   */
  dispose.stageReact=layout=>{
    const oldTemplate=graph.layout.template?.layout,newTemplate=layout.template?.layout;
    if(!(graph.layout.selections?.length||graph._fullLayout.selections?.length||oldTemplate?.selections?.length||layout.selections?.length||newTemplate?.selections?.length))return false;
    desired=copy(layout.selections);templateDesired=copy(newTemplate?.selections);
    const patch=Object.fromEntries(Object.entries(layout).filter(([key])=>selectionKey(key))),templatePatch=newTemplate&&own(newTemplate,'selections')?{input:newTemplate,value:newTemplate.selections}:null;
    pending.push({layout,patch,templatePatch});for(const key of Object.keys(patch))delete layout[key];layout.selections=[];graph.layout.selections=[];
    for(const input of [oldTemplate,newTemplate].filter(Boolean)){if(!templates.has(input))templates.set(input,copy(input.selections));input.selections=[];}
    return true;
  };
  return dispose;
}

/** Interrupt an active pinned draw before releasing its renderer state.
 * Main caller: mount's disposal event after observers/listeners stop accepting
 * user work. Native axis transitions retain a final shape-drawing callback;
 * immediate purge leaves that callback without its subplot layout. A native
 * zero-duration transition completes the interruption before purge. Selection
 * staging stays active through that transition. The native queue becomes
 * active before asynchronous frame preparation, so it also guards removal
 * before the duration flag is set. Inactive and legacy charts retain their
 * immediate cleanup path. Failed interruption still purges and
 * rejects visibly; no native rejection or global error event is suppressed.
 */
export function disposePlotlyGraph(graph,Plotly,disposeSelections) {
  const purge=()=>{disposeSelections();Plotly.purge(graph);};
  if(Plotly.version!=='4.1.1'||!(graph._transitioningWithDuration||graph._transitionData?._animationRaf)){purge();return Promise.resolve();}
  let interrupted;
  try{interrupted=Plotly.animate(graph,{data:[],layout:{}},{mode:'immediate',frame:{duration:0,redraw:true},transition:{duration:0}});}
  catch(error){purge();return Promise.reject(error);}
  return Promise.resolve(interrupted).then(purge,error=>{purge();throw error;});
}

/** Serialize retained requests and keep only the newest unstarted figure.
 * Main caller: mount; hydratePlotly submits descriptors to this queue.
 * Why this exists: native react and animation share mutable trace/layout state.
 * A failed requested fingerprint must remain retryable. Each application gets
 * a current-request predicate, so slow resource reads cannot apply an obsolete
 * figure. In-flight native work finishes before the next request starts.
 */
export function plotlyUpdateQueue(initial,{apply,report,busy=()=>{}}) {
  let requested=initial,revision=0,closed=false,pending=Promise.resolve();
  const queue={get revision(){return revision;},get pending(){return pending;},get busy(){return !closed&&requested!==undefined&&active;},close(){closed=true;revision++;active=false;busy(false);}};let active=false;
  queue.request=(entry,signature)=>{
    if(closed||signature===requested)return false;
    requested=signature;const ticket=++revision,current=()=>!closed&&ticket===revision;active=true;busy(true);
    pending=pending.then(async()=>{if(current())await apply(entry,current);}).catch(error=>{
      if(!current())return;requested=undefined;report(error,()=>queue.request(entry,signature));
    }).finally(()=>{if(current()){active=false;busy(false);}});
    return true;
  };
  return queue;
}

/** Drain the native animation before a retained figure changes its inputs.
 * Main caller: mount's native update stage. A zero-duration inline transition
 * replaces queued old frames and resolves after its own draw and queue boundary.
 * Do not restore staged selections until that native preparation has finished.
 */
export function interruptPlotlyAnimation(graph,Plotly) {
  if(!(graph._transitioningWithDuration||graph._transitionData?._animationRaf))return Promise.resolve();
  return Promise.resolve(Plotly.animate(graph,{data:[],layout:{}},{mode:'immediate',frame:{duration:0,redraw:true},transition:{duration:0}}));
}

/** Keep pending native ownership distinct from an already settled promise.
 * Main callers: mount's controls, newPlot, retained react and disposal event.
 * Why this exists: idle purge must happen while the disposal caller still owns
 * its synchronous teardown stage. Actual native work retains callbacks until
 * it settles, so its cleanup must wait. Observing settlement here never changes
 * the original promise or its error; the owning caller still reports failures.
 * Every tracked operation must chain to current first: mounted callers serialize
 * that ownership, so the latest promise also covers all earlier native work.
 */
export function plotlyNativeWork() {
  let current=Promise.resolve();const pending=new Set();
  return {
    get current(){return current;},get pending(){return pending.size>0;},
    track(value){
      const work=Promise.resolve(value);current=work;pending.add(work);
      const settled=()=>{pending.delete(work);if(current===work)current=Promise.resolve();};
      work.then(settled,settled);return work;
    },
    release(callback){return pending.size?current.then(callback,callback):callback();}
  };
}

/** Clone native number wrappers and remove only executable/resource references.
 * Main callers: preparePlotly. Text, arrays, templates and explicit colors stay
 * intact. Resource omissions are reported alongside the unchanged saved JSON.
 */
function clone(value, notices, path = []) {
  if (Array.isArray(value)) return value.map(item => clone(item, notices, path));
  if (!value || typeof value !== 'object') {
    if (typeof value === 'string' && /<\/?a(?:\s|>)/i.test(value)) {
      notices.add('Links in chart text are shown as text.');
      return value.replace(/<\/?a(?:\s[^>]*)?>/gi, '');
    }
    return value;
  }
  if (value._type === 'number' && ['NaN','Infinity','-Infinity'].includes(value.value)) return null;
  const entries = [];
  for (const [key, item] of Object.entries(value)) {
    if (['__proto__','prototype','constructor'].includes(key)) continue;
    entries.push([key, clone(item, notices, [...path,key])]);
  }
  return Object.fromEntries(entries);
}

export function preparePlotly(spec, key = 'figure') {
  const notices = new Set(), problems = [];
  if (!spec || !Array.isArray(spec.data) || spec.data.some(trace => !trace || typeof trace !== 'object' || Array.isArray(trace))) return {problems:['Figure data must be a list of trace objects.'], notices:[], figure:null};
  const figure = clone(spec, notices);
  let imageIndex=0;
  for(const trace of figure.data)if(trace.type==='image') {
    const defaults=figure.layout?.template?.data?.image || [],inherited=defaults.length ? defaults[imageIndex++ % defaults.length] : {};
    if(!trace.source && !trace.z && !inherited.source && !inherited.z)problems.push('An image trace has no source or pixel data.');
  }
  const layout = figure.layout && typeof figure.layout === 'object' ? figure.layout : {};
  // Explicit saved templates have their own defaults. Do not recolor them.
  if(typeof layout.title==='string')layout.title={text:layout.title};
  for(const [name,axis] of Object.entries(layout))if(/^[xy]axis\d*$/.test(name) && typeof axis?.title==='string')axis.title={text:axis.title};
  const template = layout.template?.layout || {};
  for (const [name, value] of Object.entries({paper_bgcolor:'#252a31',plot_bgcolor:'#252a31',colorway:['#8abbff','#6ac9d0','#b49ae8','#fbb360','#4c90f0']})) if (!own(layout,name) && !own(template,name)) layout[name] = value;
  if (!own(layout,'font') && !own(template,'font')) layout.font = {color:'#c5cbd3',family:'Arial, sans-serif',size:12};
  if (!own(layout,'margin') && !own(template,'margin')) layout.margin = {l:58,r:30,t:50,b:55};
  const geos=new Set(figure.data.filter(trace=>['scattergeo','choropleth'].includes(trace.type)).map(trace=>trace.geo || 'geo'));
  for(const name of geos){layout[name] ??={};for(const [field,color] of Object.entries({bgcolor:'#252a31',landcolor:'#383e47',oceancolor:'#252a31',lakecolor:'#252a31'}))if(!own(layout[name],field)&&!own(template[name] || {},field))layout[name][field]=color;}
  if (!own(layout,'uirevision')) layout.uirevision = key;
  figure.layout = layout;
  return {figure,notices:[...notices],problems:[...new Set(problems)]};
}

/** Reject unknown trace families before Plotly can silently coerce to scatter.
 * Main callers: mount and its update callback, after choosing the real renderer.
 * Why this exists: Plotly accepts unknown type strings by applying scatter
 * defaults. That can draw a plausible but incorrect chart from saved user data.
 * Frame types inherit the remapped parent trace, and template defaults use their
 * declared group. This checks families, not all combinations of trace options.
 */
export function validateTraceTypes(figure, Plotly) {
  const supported=Plotly.PlotSchema.get().traces;
  const check=type=>{if(!own(supported,type))throw new Error(`Trace type ${String(type)} is not supported by the local Plotly ${Plotly.version} renderer.`);};
  const named=new Map((figure.frames || []).map(frame=>[String(frame.name),frame]));
  /** Apply coordinate defaults at the resolved trace's coercion stage.
   * Main callers: initial and resolved-frame validation below. Native Plotly
   * treats null coordinates as a request for template defaults, and a frame
   * changing scatter to quiver selects the quiver template at that later stage.
   * Resolve siblings too so repeated templates cycle by the effective type.
   */
  const templated=traces=>{
    let quiverIndex=0;
    return traces.map(trace=>{
      if(trace.type!=='quiver')return trace;
      const defaults=figure.layout?.template?.data?.quiver || [],inherited=defaults.length?defaults[quiverIndex++ % defaults.length]:{};
      const resolved={...inherited,...trace};
      for(const key of ['x','y','x0','dx','y0','dy'])if(trace[key]==null&&own(inherited,key))resolved[key]=inherited[key];
      return resolved;
    });
  };
  const quiver=trace=>{
    if(trace.type!=='quiver'||!['x0','dx','y0','dy'].some(key=>own(trace,key)&&trace[key]!=null))return;
    if(trace.x==null||trace.y==null)throw new Error(`The local Plotly ${Plotly.version} quiver renderer needs explicit x and y coordinates; implicit x0/dx/y0/dy coordinates are not rendered.`);
  };
  /** Resolve the coordinate inputs at the frame's actual inheritance stage.
   * Main caller: frame validation below. A partial update can rely on parent or
   * baseframe x/y arrays, so testing the isolated fragment would reject valid
   * charts. This reads saved inputs only and never rewrites their coordinates.
   */
  const frameTrace=(frame,parent,seen=new Set())=>{
    if(seen.has(frame))throw new Error('An animation baseframe contains a cycle.');seen.add(frame);
    const base=frame.baseframe===undefined?null:named.get(String(frame.baseframe)),inherited=base?frameTrace(base,parent,seen):figure.data[parent];
    const index=frame.traces?frame.traces.indexOf(parent):parent;
    return {...inherited,...(index<0?{}:frame.data?.[index] || {})};
  };
  for(const trace of templated(figure.data)){check(trace.type || 'scatter');quiver(trace);}
  for(const [type,traces] of Object.entries(figure.layout?.template?.data || {})) {
    check(type);for(const trace of traces)if(trace.type)check(trace.type);
  }
  for(const frame of animationFrames(figure)) {
    for(const [index] of (frame.data || []).entries()) {
      const parent=frame.traces?.[index] ?? index;
      if(!Number.isInteger(parent)||parent<0||parent>=figure.data.length)throw new Error('An animation frame references a trace outside the saved figure.');
    }
    for(const trace of templated(figure.data.map((_,parent)=>frameTrace(frame,parent)))){check(trace.type || 'scatter');quiver(trace);}
  }
}

/** Keep native transitions and apply saved properties in their final redraw.
 * Main callers: mount and retained updates, after choosing the pinned renderer.
 * Why this exists: redraw=false is a performance option; Plotly then leaves
 * non-animatable layouts and trace families visually stale. Enable its own final
 * redraw only for affected controls. Native duration, easing, ordering, queue,
 * frame inheritance remain Plotly's responsibility. Native controls emit their
 * interaction events; our dispatcher catches the native animate promise so
 * expected queue cancellation does not become an unhandled browser rejection.
 * The browser copy changes; original logged SDK bytes remain unchanged.
 */
export function preparePlotlyAnimations(figure,Plotly) {
  const result=JSON.parse(JSON.stringify(figure)),frames=result.frames || [],named=new Map(frames.map(frame=>[String(frame.name),frame]));
  const schema=Plotly.PlotSchema.get().traces;
  const selectionRedraw=Plotly.version==='4.1.1'&&(result.layout?.selections?.length||result.layout?.template?.layout?.selections?.length||animationFrames(result).some(frame=>Object.keys(frame.layout || {}).some(selectionKey)||frame.layout?.template?.layout?.selections?.length));
  const needsRedraw=(frame,seen=new Set())=> {
    if(!frame)return false;
    if(seen.has(frame))throw new Error('An animation baseframe contains a cycle.');seen.add(frame);
    if(frame.baseframe!==undefined&&!named.has(String(frame.baseframe)))throw new Error('An animation baseframe references a missing saved frame.');
    const inherited=frame.baseframe===undefined?false:needsRedraw(named.get(String(frame.baseframe)),seen);
    return inherited || Object.keys(frame.layout || {}).length>0 || (frame.data || []).some((trace,index)=> {
      const type=trace.type || result.data[frame.traces?.[index] ?? index]?.type || 'scatter';
      return !Object.entries(schema[type]?.attributes || {}).some(([key,value])=>!['uid','ids'].includes(key)&&value?.anim);
    });
  };
  let changed=false;
  const controls=[...(result.layout?.updatemenus || []).flatMap(menu=>menu.buttons || []),...(result.layout?.sliders || []).flatMap(slider=>slider.steps || [])];
  for(const control of controls)if(control.method==='animate'&&control.execute!==false)for(const args of [control.args,control.args2].filter(Array.isArray)) {
    const [selection,options]=args;
    const selected=selection==null?frames:['string','number'].includes(typeof selection)?frames.filter(frame=>String(frame.group)===String(selection)):Array.isArray(selection)?selection.map(value=> {
      if(!['string','number'].includes(typeof value))return value;
      const frame=named.get(String(value));if(!frame)throw new Error('An animation control references a missing saved frame.');return frame;
    }):[selection];
    // A reversed/fromcurrent sequence can change the options-array indexes.
    // Keep all timing entries and the renderer's index/fallback rules intact.
    if(selectionRedraw||selected.some(frame=>needsRedraw(frame)))for(const settings of Array.isArray(options?.frame)?options.frame:[options?.frame])if(settings?.redraw===false) {
      settings.redraw=true;changed=true;
    }
  }
  const take=control=>{const active=control.method==='animate'&&control.execute!==false;if(active)control.execute=false;return active;};
  const controlsOwned={buttons:(result.layout?.updatemenus || []).map(menu=>(menu.buttons || []).map(take)),sliders:(result.layout?.sliders || []).map(slider=>(slider.steps || []).map(take))};
  return {figure:result,controls:controlsOwned,notices:changed?['Final frame redraw is enabled to apply saved layout and trace updates.']:[]};
}

const libraries=new Map();let loading=Promise.resolve();
const mounted = new WeakMap();
/** Load pinned renderers serially and retain their independent instances.
 * Main callers: mount. Plotly4 removed Mapbox traces, so genuine3.6 handles
 * those figures without rewriting legacy attributes, styles or credentials.
 */
function library(version) {
  if(libraries.has(version))return libraries.get(version);
  const pending=loading.catch(()=>{}).then(()=>new Promise((resolve,reject)=> {
    if(globalThis.Plotly?.version===version){resolve(globalThis.Plotly);return;}
    const script=document.createElement('script');script.src=new URL(`./plotly-strict-${version}.min.js`,import.meta.url).href;
    script.onload=()=>globalThis.Plotly?.version===version?resolve(globalThis.Plotly):reject(new Error('Unexpected Plotly renderer version.'));
    script.onerror=()=>reject(new Error('The local Plotly renderer could not be loaded.'));document.head.append(script);
  })).catch(error=>{libraries.delete(version);throw error;});
  libraries.set(version,pending);loading=pending;return pending;
}
const rendererVersion=entry=>usesLegacyMap(entry.spec)?'3.6.0':'4.1.1';
const fingerprint=entry=>JSON.stringify([entry.spec,entry.resources]);
const element = (tag, className, text) => {const node=document.createElement(tag);node.className=className;if(text!==undefined)node.textContent=text;return node;};

/** Hydrate only new charts; preserved nodes retain zoom, legend and animation.
 * Main callers: app hydrateMedia after every render. Disposal aborts pending
 * display work and releases Plotly listeners/canvases and the resize observer.
 */
export function hydratePlotly(root, registry, options={}) {
  for (const container of root.querySelectorAll('[data-plotly-id]')) {
    const entry=registry.get(container.dataset.plotlyId); if(!entry)continue;
    let current=mounted.get(container);const signature=fingerprint(entry);
    if(current && current.version!==rendererVersion(entry)){container.dispatchEvent(new Event('backprop:dispose'));delete container.dataset.plotlyReady;current=null;}
    if(current)current.updates.request(entry,signature);
    if(!current && container.dataset.plotlyFingerprint!==signature)delete container.dataset.plotlyReady;
    if (container.dataset.plotlyReady || container.dataset.plotlyLoading) continue;
    container.dataset.plotlyFingerprint=signature;container.dataset.plotlyLoading='true';
    mount(container,entry,options).then(()=>{if(container.isConnected)hydratePlotly(root,registry,options);}).catch(error=> {
      if (!container.isConnected) return;
      container.dispatchEvent(new Event('backprop:dispose'));
      const latest=registry.get(container.dataset.plotlyId);
      if(latest&&fingerprint(latest)!==signature){delete container.dataset.plotlyLoading;delete container.dataset.plotlyReady;hydratePlotly(root,registry,options);return;}
      delete container.dataset.plotlyLoading;container.dataset.plotlyReady='error';
      const notice=element('div','notice error',`Could not render this Plotly figure: ${error.message}`);notice.setAttribute('role','alert');
      const retry=element('button','btn small','Retry chart');retry.addEventListener('click',()=>{delete container.dataset.plotlyReady;hydratePlotly(root,registry,options);});
      container.replaceChildren(notice,retry);
    });
  }
}
async function mount(container,entry,options) {
  let prepared=preparePlotly(await resolvePlotlyResources(entry,options),entry.id);
  if (prepared.problems.length) {
    container.replaceChildren(element('div','notice',prepared.problems.join(' ')));
    container.dataset.plotlyReady='true';delete container.dataset.plotlyLoading;return;
  }
  const version=rendererVersion(entry),Plotly=await library(version); if(!container.isConnected)return;
  validateTraceTypes(prepared.figure,Plotly);
  const animation=preparePlotlyAnimations(prepared.figure,Plotly);prepared.figure=animation.figure;prepared.notices.push(...animation.notices);
  const graph=element('div','plotly-graph'), actions=element('div','plotly-actions'), reported=new Map();
  const report=(error,action,retry)=> {
    if(!container.isConnected)return;if(retry){reported.get(action)?.remove();reported.delete(action);}if(reported.has(action))return;
    const notice=element('div','notice error',`Could not ${action}: ${error.message}`);notice.setAttribute('role','alert');reported.set(action,notice);
    if(retry){const button=element('button','btn small','Retry chart update');button.addEventListener('click',()=>{notice.remove();reported.delete(action);retry();});notice.append(button);}container.append(notice);
  };
  const perform=(action,callback)=>{
    const revision=updates?.revision;
    return nativeWork.track(nativeWork.current.then(()=>{if(!disposed&&!updates?.busy&&revision===updates?.revision)return callback();}).catch(error=>report(error,action)));
  };
  const size=layout=> {graph.style.width=Number.isFinite(layout.width)&&layout.width>0?`${layout.width}px`:'100%';graph.style.height=Number.isFinite(layout.height)&&layout.height>0?`${layout.height}px`:'';};
  size(prepared.figure.layout);
  graph.tabIndex=0;graph.setAttribute('role','region');graph.setAttribute('aria-label','Interactive Plotly figure');
  let viewReset={};const savedLayout=JSON.parse(JSON.stringify(prepared.figure.layout));
  const reset=element('button','btn small',prepared.figure.data.some(trace=>/geo|map|3d|polar|ternary|smith|^choropleth$|^(surface|cone|streamtube|volume|isosurface)$/.test(trace.type || ''))?'Reset view':'Reset axes');reset.addEventListener('click',()=> {
    perform('reset chart view',()=>Plotly.relayout(graph,viewReset));
  });
  const download=element('button','btn small','Download PNG');download.addEventListener('click',()=>perform('download chart image',()=>Plotly.downloadImage(graph,{format:'png',filename:'backprop-figure'})));
  actions.append(reset,download,element('span','dim','Drag to zoom · Double-click to reset'));
  container.replaceChildren(...prepared.notices.map(text=>element('div','notice',text)),graph,actions);
  let disposed=false,observer,disposeSelections=()=>{},updates;const nativeWork=plotlyNativeWork();
  let animationControls=animation.controls;const runningAnimations=new Set();
  container.addEventListener('backprop:dispose',event=> {
    disposed=true;updates?.close();runningAnimations.clear();observer?.disconnect();mounted.delete(container);
    // Native react/newPlot owns callbacks until its promise completes. Resource
    // reads have no native ownership and can finish later without applying.
    // mount/update already owns reporting a rejected native operation. Cleanup
    // still runs after it settles; an actual interruption failure propagates.
    // A lifecycle caller may adopt real asynchronous cleanup via waitUntil.
    // Ordinary replacement still purges idle charts before disconnecting them;
    // pending native operations keep their exact observable cleanup promise.
    const cleanup=nativeWork.release(()=>disposePlotlyGraph(graph,Plotly,disposeSelections));
    event.detail?.waitUntil?.(Promise.resolve(cleanup));
  },{once:true});
  let savedTraces=prepared.figure.data.map(trace=>({uid:trace.uid,name:trace.name,type:trace.type,visible:trace.visible}));
  let mapFailure;const observedMaps=new WeakSet();
  function bindMapErrors() {
    for(const [key,subplot] of Object.entries(graph._fullLayout || {}))if(/^(map|mapbox)\d*$/.test(key)) {
      const map=subplot?._subplot?.map;if(!map||observedMaps.has(map))continue;observedMaps.add(map);
      map.on('error',event=> {
        const detail=String(event.error?.message || ''),status=event.error?.status;
        mapFailure=new Error(/decode|image type|image format/i.test(detail)?'A map image could not be decoded. Check the saved image format and integrity.':`A map resource could not load${Number.isInteger(status)?` (HTTP ${status})`:''}. Check provider token, CORS and map_resource_origins hosting policy.`);
        if(container.dataset.plotlyReady)report(mapFailure,'load map layer');
      });
    }
  }
  // Map creation loads resources before newPlot resolves. Subscribe during that
  // stage too, so initial decode/provider failures do not become opaque errors.
  const watchMaps=setInterval(bindMapErrors,10);
  try{const plotting=nativeWork.track(Plotly.newPlot(graph,prepared.figure.data,prepared.figure.layout,{responsive:false,displaylogo:false,displayModeBar:'hover',scrollZoom:false,editable:false,showSendToCloud:false,topojsonURL:new URL('./',import.meta.url).href}));bindMapErrors();await plotting;}
  catch(error){throw mapFailure || error;}finally{clearInterval(watchMaps);}

  // replaceView normally sends disposal before disconnecting the container.
  // A direct DOM removal still releases a completed newPlot through that same
  // ownership boundary instead of leaving native state attached to a dead node.
  if(!container.isConnected&&!disposed)container.dispatchEvent(new Event('backprop:dispose'));
  if(disposed)return;
  viewReset=plotlyResetPatch(savedLayout,graph._fullLayout);
  disposeSelections=bindPlotlyFrameSelections(graph,Plotly);
  if(prepared.figure.frames?.length)await Plotly.addFrames(graph,prepared.figure.frames);
  /** Execute saved native animation arguments with a lifecycle-aware catch.
   * Main callers: native Plotly buttonclicked and sliderchange below. Plotly
   * intentionally rejects a superseded queue without an Error; its built-in UI
   * leaves that rejection unhandled. Only an observed queue interruption or
   * chart disposal is a normal cancellation. Other failures remain visible.
   */
  function animate(args) {
    const ticket={interrupted:false};let promise;
    try{promise=Plotly.animate(graph,...(Array.isArray(args)?args:[]));runningAnimations.add(ticket);}
    catch(error){if(!runningAnimations.size)disposeSelections.restore?.();report(error,'animate chart');return;}
    Promise.resolve(promise).catch(error=> {
      if(disposed || (error===undefined&&ticket.interrupted))return;
      if(runningAnimations.size===1)disposeSelections.restore?.();
      report(error instanceof Error?error:new Error(error===undefined?'The local animation did not complete.':String(error)),'animate chart');
    }).finally(()=>runningAnimations.delete(ticket));
  }
  graph.on('plotly_animationinterrupted',()=>{for(const ticket of runningAnimations)ticket.interrupted=true;});
  graph.on('plotly_buttonclicked',event=> {
    if(disposed||updates?.busy)return;const revision=updates?.revision;
    // Native array indexes survive supplyDefaults; object identities do not.
    const menu=event.menu._index,button=event.button._index;
    if(!Number.isInteger(menu)||!Number.isInteger(button)){report(new Error('The local animation control has no saved index.'),'animate chart');return;}
    if(!animationControls.buttons[menu]?.[button])return;
    const toggled=Boolean(event.button.args2)&&graph._fullLayout.updatemenus[menu].active===button,args=toggled?event.button.args2:event.button.args;
    perform('change animation control',()=>Plotly.relayout(graph,{[`updatemenus[${menu}].active`]:toggled?-1:button}).then(()=>{if(!disposed&&!updates?.busy&&revision===updates?.revision)animate(args);}));
  });
  graph.on('plotly_sliderchange',event=> {
    if(disposed||updates?.busy)return;
    const slider=event.slider._index,step=event.step._index;
    if(!Number.isInteger(slider)||!Number.isInteger(step)){report(new Error('The local animation control has no saved index.'),'animate chart');return;}
    if(event.interaction&&animationControls.sliders[slider]?.[step])animate(event.step.args);
  });
  bindMapErrors();
  const legend=element('div','plotly-accessible-legend');legend.setAttribute('aria-label','Plotly trace visibility');
  function updateLegend() {
    const previous=document.activeElement?.dataset.plotlyTrace;
    legend.replaceChildren();
    graph.data.forEach((trace,index)=> {
      if(trace.visible===false || trace.showlegend===false)return;
      const shown=trace.visible!=='legendonly',label=trace.name || `Trace ${index+1}`;
      const button=element('button','btn small'+(shown?'':' muted-series'),`${shown?'Hide':'Show'} ${label}`);
      button.dataset.plotlyTrace=String(index);button.setAttribute('aria-pressed',String(shown));
      button.addEventListener('click',()=>perform('change trace visibility',()=>Plotly.restyle(graph,{visible:shown?'legendonly':true},[index])));legend.append(button);
    });
    if(previous!==undefined)legend.querySelector(`[data-plotly-trace="${previous}"]`)?.focus({preventScroll:true});
  }
  graph.on('plotly_restyle',updateLegend);updateLegend();container.append(legend);
  // ResizeObserver also catches sidebar changes and retained charts moved into
  // different width containers, which window-resize listeners cannot detect.
  let width=0;
  observer=new ResizeObserver(()=> {if(!disposed && graph.isConnected && Math.abs(graph.clientWidth-width)>1){width=graph.clientWidth;perform('resize chart',()=>Plotly.Plots.resize(graph));}});observer.observe(graph);
  graph.querySelectorAll('.modebar-btn').forEach(button=> {button.tabIndex=0;button.setAttribute('role','button');button.addEventListener('keydown',event=> {if(['Enter',' '].includes(event.key)){event.preventDefault();button.click();}});});
  const busy=value=>{container.setAttribute('aria-busy',String(value));graph.style.pointerEvents=value?'none':'';for(const button of container.querySelectorAll('button'))button.disabled=value;};
  updates=plotlyUpdateQueue(fingerprint(entry),{busy,report:(error,retry)=>report(error,'update figure; displayed data may be incomplete',retry),apply:async(next,current)=> {
    if(disposed || !container.isConnected)return;
    const changed=preparePlotly(await resolvePlotlyResources(next,options),next.id);if(changed.problems.length)throw new Error(changed.problems.join(' '));
    if(!current()||disposed)return;
    validateTraceTypes(changed.figure,Plotly);
    const animation=preparePlotlyAnimations(changed.figure,Plotly);changed.figure=animation.figure;changed.notices.push(...animation.notices);
    const nextSavedLayout=JSON.parse(JSON.stringify(changed.figure.layout));
    const nextSaved=changed.figure.data.map(trace=>({uid:trace.uid,name:trace.name,type:trace.type,visible:trace.visible}));
    changed.figure.data.forEach((trace,index)=> {
      const prior=savedTraces[index],current=graph.data[index];
      if(prior && current && prior.visible===trace.visible && (prior.uid || prior.name || index)===(trace.uid || trace.name || index) && prior.type===trace.type) trace.visible=current.visible;
    });
    // The pending promise covers only native ownership, not resource reads.
    // Disposal waits this stage, and newer requests wait the same native work.
    const previousNative=nativeWork.current,applying=(async()=>{
    await previousNative;
    for(const ticket of runningAnimations)ticket.interrupted=true;
    await interruptPlotlyAnimation(graph,Plotly);runningAnimations.clear();
    if(!current()||disposed)return;
    size(changed.figure.layout);disposeSelections.restore?.();
    const staged=disposeSelections.stageReact?.(changed.figure.layout);
    try{await Plotly.react(graph,changed.figure.data,changed.figure.layout,graph._context);}finally{disposeSelections.restore?.();}
    if(!current()||disposed)return;
    if(staged)await Plotly.redraw(graph);if(!current()||disposed)return;bindMapErrors();
    if(!current()||disposed)return;
    animationControls=animation.controls;
    viewReset=plotlyResetPatch(nextSavedLayout,graph._fullLayout,viewReset);
    savedTraces=nextSaved;
    if(graph._transitionData?._frames?.length)await Plotly.deleteFrames(graph,graph._transitionData._frames.map((_,index)=>index));
    if(!current()||disposed)return;
    if(changed.figure.frames?.length)await Plotly.addFrames(graph,changed.figure.frames);
    if(!current()||disposed)return;
    prepared=changed;updateLegend();
    container.querySelectorAll(':scope > .notice').forEach(node=>node.remove());reported.clear();
    for(const text of changed.notices)container.prepend(element('div','notice',text));
    })();await nativeWork.track(applying);
  }});
  mounted.set(container,{version,updates});
  container.dataset.plotlyReady='true';delete container.dataset.plotlyLoading;
}
