/**
 * Architecture: weak native context observations -> explicit probe/purge loss
 * requests -> loss events and connected-canvas snapshots -> exact console
 * classification. Rendering, geometry and timing assertions stay independent.
 * Main callers: plotly_animation_browser_qa and focused classification tests.
 * Why this exists: Firefox reports deprecations and intentional native teardown
 * as warnings. Treating every GPU message as failure confuses those stages with
 * context pressure, broken shaders, or loss of a chart that is still in use.
 */

/** Install observations before any native renderer creates a canvas.
 * Main caller: Playwright addInitScript. References to contexts remain weak;
 * explicit loseContext requests are recorded, never invented or forced here.
 * The caller scopes only its real diagnostic probe or native purge operation.
 */
export function installPlotlyGpuObserver() {
  const original=HTMLCanvasElement.prototype.getContext,seen=new WeakMap(),records=[];
  const stage=()=>window.gpuPhase || 'render';
  HTMLCanvasElement.prototype.getContext=function(...args) {
    const context=original.apply(this,args);
    if(!context||!/^webgl|experimental-webgl$/.test(args[0])||seen.has(context))return context;
    const canvas=this,record={id:records.length,created_case:window.qaCase || 'boot',context:new WeakRef(context),canvas:new WeakRef(canvas),debug_requests:[],loss_requests:[],losses:[]};
    seen.set(context,record.id);records.push(record);
    const getExtension=context.getExtension.bind(context);
    context.getExtension=function(name) {
      const extension=getExtension(name);
      if(name==='WEBGL_debug_renderer_info')record.debug_requests.push({time:performance.now(),phase:stage()});
      if(name==='WEBGL_lose_context'&&extension&&!extension.qaObserved) {
        const lose=extension.loseContext.bind(extension);extension.qaObserved=true;
        extension.loseContext=function(...args) {record.loss_requests.push({time:performance.now(),phase:stage(),connected:canvas.isConnected});return lose(...args);};
      }
      return extension;
    };
    canvas.addEventListener('webglcontextlost',()=>record.losses.push({time:performance.now(),connected:canvas.isConnected,request:record.loss_requests.length?record.loss_requests.length-1:null}));
    return context;
  };
  window.gpuSnapshot=()=>records.map(record=> {
    const context=record.context.deref();
    return {id:record.id,created_case:record.created_case,collected:!context,lost:context?.isContextLost(),connected:Boolean(record.canvas.deref()?.isConnected),lost_events:record.losses.length,debug_requests:record.debug_requests.map(value=>({...value})),loss_requests:record.loss_requests.map(value=>({...value})),losses:record.losses.map(value=>({...value}))};
  });
}

/** Classify exact native notices only after checking independent loss evidence.
 * Main callers: the final browser gate and deterministic negative tests. A
 * restored active-context loss still fails because its event remains recorded.
 * Unknown GPU warnings and all GPU error messages fail closed. Known native
 * portability/deprecation notices remain visible with their full raw message.
 */
export function classifyPlotlyGpu(messages,lifecycle) {
  const snapshots=lifecycle.snapshots || [],final=lifecycle.final || [],contexts=new Map(),errors=[],classified=[];
  for(const snapshot of [...snapshots,{contexts:final}])for(const context of snapshot.contexts) {
    contexts.set(context.id,context);
    if(context.connected&&context.lost)errors.push({kind:'active_context_lost',id:context.id,case:snapshot.case});
    if(context.connected&&context.collected)errors.push({kind:'active_context_collected',id:context.id,case:snapshot.case});
  }
  let expectedLosses=0,probeDebug=0;
  for(const context of contexts.values()) {
    probeDebug+=(context.debug_requests || []).filter(request=>request.phase==='probe').length;
    for(const loss of context.losses || []) {
      const request=context.loss_requests?.[loss.request];
      if(loss.connected)errors.push({kind:'active_context_loss_event',id:context.id,loss});
      if(request&&['probe','teardown'].includes(request.phase)&&request.time<=loss.time&&!loss.connected&&(request.phase!=='probe'||!request.connected))expectedLosses++;
      else errors.push({kind:'unexplained_context_loss',id:context.id,loss});
    }
  }
  const body=text=>text.match(/^\[JavaScript Warning: "([^\"]+)"(?: \{file: "[^\"]*" line: \d+\})?\]$/)?.[1] || text;
  const debug='WEBGL_debug_renderer_info is deprecated in Firefox and will be removed. Please use RENDERER.';
  const upload='WebGL warning: texImage: Alpha-premult and y-flip are deprecated for non-DOM-Element uploads.';
  const portability='WebGL warning: checkFramebufferStatus: Using format enabled by implicitly enabled extension: WEBGL_color_buffer_float. For maximal portability enable it explicitly.';
  const cap='After reporting 32, no further warnings will be reported for this WebGL context.';
  let lossNotices=0,debugNotices=0,knownNativeNotices=0;const capped=[];
  for(const [index,message] of messages.entries()) {
    if(!/WebGL|GL_|shader|framebuffer|context.{0,30}lost/i.test(message.text))continue;
    const text=body(message.text);let category;
    if(message.type==='warning') {
      if(text===debug&&++debugNotices<=probeDebug)category='observed_probe_deprecation';
      else if(text==='WebGL context was lost.'&&++lossNotices<=expectedLosses)category='observed_probe_or_teardown_loss';
      else if(text===upload){category='native_upload_deprecation';knownNativeNotices++;}
      else if(text===portability){category='native_float_portability_notice';knownNativeNotices++;}
      else if(text===cap){category='native_warning_limit_notice';capped.push(index);}
    }
    if(!category)errors.push({kind:'gpu_console_failure',index,message});
    classified.push({index,category:category || 'failure',message});
  }
  if(capped.length&&(knownNativeNotices<32||errors.length))for(const index of capped)errors.push({kind:'unexplained_warning_limit',index});
  return {passed:errors.length===0,errors,classified,expected_loss_events:expectedLosses,observed_probe_debug_requests:probeDebug,warning_limit_notices:capped.length};
}
