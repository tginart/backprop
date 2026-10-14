/**
 * Architecture: native queue completion -> renderer/D3 work observation ->
 * unchanged drawn geometry across a paint -> exact final comparison. Map
 * canvases additionally need their own native idle event before compositor paint.
 * Main callers: plotly_animation_browser_qa and focused deterministic tests.
 * Why this exists: plotly_animated reports a drained frame queue. Its timer can
 * run before the final SVG tween paint, especially in independent engines.
 * This helper observes completion without redrawing or altering the figure.
 */

/** Read the pinned renderer's active drawing work without changing it.
 * Main caller: waitForPlotlyDrawing and queue-end diagnostic recording.
 * D3 stores pending SVG transitions on their nodes until its final tween ends.
 */
export function plotlyDrawingWork(graph) {
  let svgTransitions=0;
  for(const node of graph.querySelectorAll('svg *'))for(const key of Object.getOwnPropertyNames(node))if(key.startsWith('__transition')) {
    const state=node[key];
    if(state&&typeof state==='object'&&(state.count>0||Object.keys(state).some(name=>/^\d+$/.test(name))))svgTransitions++;
  }
  return {transitioning:Boolean(graph._transitioning),queue:Boolean(graph._transitionData?._animationRaf),svgTransitions};
}

/** Wait for native drawing to finish, then read unchanged geometry after paint.
 * Main caller: the paired interpolation browser observation. A stuck tween or
 * renderer state fails with a bounded diagnostic; no tolerance hides mismatch.
 * Clock and paint callbacks are injectable only for deterministic Node tests.
 */
export async function waitForPlotlyDrawing(graphs,readGeometry,{timeoutMs=5000,paint=()=>new Promise(resolve=>requestAnimationFrame(resolve)),now=()=>performance.now()}={}) {
  const started=now(),initial=graphs.map(plotlyDrawingWork);let previous,paints=0;
  while(now()-started<=timeoutMs) {
    await paint();paints++;
    const work=graphs.map(plotlyDrawingWork),geometry=graphs.map(readGeometry);
    if(work.every(state=>!state.transitioning&&!state.queue&&!state.svgTransitions)) {
      const current=JSON.stringify(geometry);
      if(current===previous)return {geometry,initial,paints,elapsed:now()-started};
      previous=current;
    }else previous=undefined;
  }
  throw new Error('Native Plotly drawing did not settle: '+JSON.stringify(graphs.map(plotlyDrawingWork)));
}

/** Read map readiness and actual native GPU state without modifying the plot.
 * Main callers: paired map-paint observation and browser failure diagnostics.
 * Why this exists: a correctly populated fullData object does not prove that
 * the native heatmap shader drew its canvas. Source/layer IDs omit resource URLs.
 */
export function plotlyMapState(graph) {
  return Object.entries(graph._fullLayout || {}).flatMap(([subplot,value])=> {
    const map=value?._subplot?.map;if(!map)return [];
    const style=map.getStyle(),canvas=map.getCanvas(),gl=map.painter?.context?.gl;
    return [{subplot,loaded:map.loaded(),style_loaded:map.isStyleLoaded(),sources:Object.keys(style.sources || {}).map(id=>({id,loaded:map.isSourceLoaded(id)})),layers:(style.layers || []).map(layer=>({id:layer.id,type:layer.type})),canvas:{width:canvas.width,height:canvas.height,client_width:canvas.clientWidth,client_height:canvas.clientHeight},gpu:gl?{version:gl.getParameter(gl.VERSION),renderer:gl.getParameter(gl.RENDERER),context_lost:gl.isContextLost(),float_color_buffer:Boolean(gl.getExtension('EXT_color_buffer_float')),half_float_color_buffer:Boolean(gl.getExtension('EXT_color_buffer_half_float'))}:null}];
  });
}

/** Wait for each existing native map to render and become idle, then paint.
 * Main callers: browser initial map capture and post-control capture. Plotly's
 * newPlot promise resolves on a loaded render; native map idle and browser
 * compositing are later stages. Repaint requests do not redraw or change data.
 * A missing idle event fails within five seconds rather than hiding a blank map.
 */
export async function waitForPlotlyMapPaint(graphs,{timeoutMs=5000,paint=()=>new Promise(resolve=>requestAnimationFrame(resolve)),now=()=>performance.now()}={}) {
  const initial=graphs.map(plotlyMapState),started=now(),observations=[];
  await Promise.all(graphs.flatMap((graph,index)=>Object.entries(graph._fullLayout || {}).flatMap(([subplot,value])=> {
    const map=value?._subplot?.map;if(!map)return [];
    return [new Promise((resolve,reject)=> {
      const record={graph:index,subplot,renders:0,idle:false};observations.push(record);
      const rendered=()=>record.renders++,cleanup=()=>{clearTimeout(timer);map.off('render',rendered);map.off('idle',idle);};
      const idle=()=>{cleanup();record.idle=true;record.elapsed=now()-started;resolve();};
      const timer=setTimeout(()=>{cleanup();reject(new Error('Native Plotly map did not become idle: '+JSON.stringify({record,state:plotlyMapState(graph)})));},timeoutMs);
      map.on('render',rendered);map.once('idle',idle);map.triggerRepaint();
    })];
  })));
  await paint();await paint();
  const final=graphs.map(plotlyMapState);
  if(final.some(maps=>maps.some(map=>!map.loaded||!map.style_loaded||map.sources.some(source=>!source.loaded)||map.gpu?.context_lost)))throw new Error('Native Plotly map paint has an unready canvas: '+JSON.stringify(final));
  return {initial,observations,final,elapsed:now()-started};
}
