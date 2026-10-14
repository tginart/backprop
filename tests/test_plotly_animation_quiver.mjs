/**
 * Architecture: saved coordinate presence -> template/baseframe inheritance ->
 * bounded renderer validation. Main caller: focused Node and release discovery.
 * Why this exists: pinned quiver silently blanks implicit-coordinate requests;
 * explicit, inherited and intentionally empty coordinates must remain accepted.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import {validateTraceTypes} from '../web/plotly-view.mjs';
const renderer={version:'4.1.1',PlotSchema:{get:()=>({traces:{scatter:{},quiver:{}}})}};
const coordinates={x:[0,1],y:[0,1],u:[1,1],v:[1,1]};
const check=figure=>validateTraceTypes(figure,renderer);

test('quiver implicit-coordinate requests fail visibly without modifying saved values',()=>{
 for(const trace of [{type:'quiver',u:[1],v:[1],x0:0,dx:1,y0:0,dy:1},
  {type:'quiver',x:[0],u:[1],v:[1],y0:0},
  {type:'quiver',y:[0],u:[1],v:[1],x0:0},
  {type:'quiver',x:null,y:[0],x0:0}]){
  const figure={data:[trace]},saved=JSON.stringify(figure);assert.throws(()=>check(figure),/needs explicit x and y coordinates/);assert.equal(JSON.stringify(figure),saved);
 }
});

test('explicit, binary, empty and absent coordinate inputs are not over-rejected',()=>{
 for(const trace of [{type:'quiver',...coordinates,x0:0,dx:1,y0:0,dy:1},
  {type:'quiver',x:{dtype:'f8',bdata:'AAAAAAAAAAA='},y:{dtype:'f8',bdata:'AAAAAAAAAAA='},u:[1],v:[1],x0:0},
  {type:'quiver',x:[],y:[],x0:0,y0:0},{type:'quiver'},
  {type:'quiver',u:[1],v:[1]}])assert.doesNotThrow(()=>check({data:[trace]}));
});

test('quiver coordinate validation uses templates and remapped partial-frame parents',()=>{
 assert.doesNotThrow(()=>check({data:[{type:'quiver',...coordinates}],layout:{template:{data:{quiver:[{x0:0,y0:0}]}}}}));
 assert.doesNotThrow(()=>check({data:[{type:'quiver',x0:0,y0:0}],layout:{template:{data:{quiver:[coordinates]}}}}));
 const source={data:[{type:'scatter'},{type:'quiver',...coordinates}],frames:[{name:'next',traces:[1],data:[{type:'quiver',x0:2,dx:2,y0:2,dy:2}]}]};
 assert.doesNotThrow(()=>check(source));source.frames[0].data[0].x=null;assert.throws(()=>check(source),/needs explicit x and y/);
});

test('quiver partial frames inherit explicit baseframe coordinates',()=>{
 const source={data:[{type:'quiver'}],frames:[{name:'base',traces:[0],data:[{type:'quiver',...coordinates}]},
  {name:'next',baseframe:'base',traces:[0],data:[{type:'quiver',x0:2,dx:2,y0:2,dy:2}]}]};
 assert.doesNotThrow(()=>check(source));source.frames[1].data[0].y=null;assert.throws(()=>check(source),/needs explicit x and y/);
});

test('null coordinates use real template defaults after effective frame type resolution',()=>{
 const template={data:{quiver:[coordinates]}};
 for(const source of [
  {data:[{type:'quiver',u:[1,1],v:[1,1],x:null,y:null,x0:0,y0:0}],layout:{template}},
  {data:[{type:'scatter',y:[0,1]}],layout:{template},frames:[{name:'next',data:[{type:'quiver',u:[1,1],v:[1,1],x0:0,y0:0}]}]},
 ]){const saved=JSON.stringify(source);assert.doesNotThrow(()=>check(source));assert.equal(JSON.stringify(source),saved);}
});

test('baseframe type inheritance rejects null coordinates without a template',()=>{
 const source={data:[{type:'scatter',x:[0,1],y:[0,1]}],frames:[{name:'base',data:[{type:'quiver',...coordinates}]},
  {name:'next',baseframe:'base',data:[{x:null,y:null,x0:0,y0:0}]}]};
 const saved=JSON.stringify(source);assert.throws(()=>check(source),/needs explicit x and y coordinates/);assert.equal(JSON.stringify(source),saved);
 source.layout={template:{data:{quiver:[coordinates]}}};assert.doesNotThrow(()=>check(source));
});

test('effective quiver siblings select their own cycling coordinate templates',()=>{
 const source={data:[{type:'scatter',y:[0]},{type:'quiver',u:[1],v:[1],x0:0,y0:0}],
  layout:{template:{data:{quiver:[{x:[0],y:[0]},{x:[1],y:[1]}]}}},
  frames:[{name:'next',data:[{type:'quiver',u:[1],v:[1],x0:0,y0:0},{}]}]};
 assert.doesNotThrow(()=>check(source));source.layout.template.data.quiver[1].x=null;assert.throws(()=>check(source),/needs explicit x and y coordinates/);
});
