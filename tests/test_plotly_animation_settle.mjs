/** Renderer queue and final SVG paint are distinct lifecycle stages.
 * Architecture: deterministic fake paint sequence -> real settlement helper ->
 * strict final geometry and explicit timeout. Main caller: focused Node tests.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import {plotlyDrawingWork,waitForPlotlyDrawing,waitForPlotlyMapPaint} from './plotly_animation_settle.mjs';

test('queue completion cannot substitute for the final SVG tween paint',async()=> {
 const svg={__transition__:{count:1,active:1,1:{}}},graph={_transitioning:false,_transitionData:{_animationRaf:null},querySelectorAll:()=>[svg]},reference={...graph,querySelectorAll:()=>[]};
 let time=0,frame=0,value=[98.71475982666016,55.665924072265625,321.2852478027344,224.33407592773438];
 assert.equal(plotlyDrawingWork(graph).svgTransitions,1);
 const result=await waitForPlotlyDrawing([graph,reference],node=>node===graph?value:[98,55,322,225],{now:()=>time,paint:async()=>{time+=16;frame++;if(frame===2){delete svg.__transition__;value=[98,55,322,225];}}});
 assert.equal(result.paints,3);assert.deepEqual(result.geometry,[[98,55,322,225],[98,55,322,225]]);assert.equal(result.initial[0].svgTransitions,1);
});

test('renderer work and permanently stuck D3 transitions fail explicitly',async()=> {
 const graph={_transitioning:true,_transitionData:{_animationRaf:1},querySelectorAll:()=>[{__transition:{1:{}}}]};let time=0;
 assert.deepEqual(plotlyDrawingWork(graph),{transitioning:true,queue:true,svgTransitions:1});
 await assert.rejects(waitForPlotlyDrawing([graph],()=>[1],{timeoutMs:30,now:()=>time,paint:async()=>{time+=16;}}),/did not settle.*svgTransitions/);
});

/** Model map events separately from loaded state, as the native renderer does.
 * Main callers: the two bounded map-observation tests below. Loaded can already
 * be true before the next native idle event and the compositor's two paints.
 */
function mapGraph(repaint) {
 const listeners=new Map(),map={loaded:()=>true,isStyleLoaded:()=>true,isSourceLoaded:()=>true,getStyle:()=>({sources:{trace:{}},layers:[{id:'density',type:'heatmap'}]}),getCanvas:()=>({width:440,height:340,clientWidth:440,clientHeight:340}),on:(name,callback)=>listeners.set(name,callback),once:(name,callback)=>listeners.set(name,callback),off:(name,callback)=>{if(listeners.get(name)===callback)listeners.delete(name);},triggerRepaint:()=>repaint(listeners)};
 return {graph:{_fullLayout:{mapbox:{_subplot:{map}}}},listeners};
}

test('a loaded map still needs a native idle event followed by two paint observations',async()=> {
 let painted=0,idle=false;
 const {graph,listeners}=mapGraph(events=>queueMicrotask(()=>{events.get('render')();idle=true;events.get('idle')();}));
 const result=await waitForPlotlyMapPaint([graph],{paint:async()=>{assert(idle);painted++;}});
 assert.equal(painted,2);assert.equal(result.initial[0][0].loaded,true);assert.equal(result.observations[0].renders,1);assert.equal(result.observations[0].idle,true);assert.equal(result.final[0][0].layers[0].type,'heatmap');assert.equal(listeners.size,0);
});

test('a loaded map that never idles fails boundedly and removes its observation listeners',async()=> {
 const {graph,listeners}=mapGraph(()=>{});
 await assert.rejects(waitForPlotlyMapPaint([graph],{timeoutMs:5,paint:async()=>assert.fail('An unready map cannot reach capture.')}),/Native Plotly map did not become idle/);
 assert.equal(listeners.size,0);
});
