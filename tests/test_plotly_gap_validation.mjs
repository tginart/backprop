/** Renderer families are validated independently of figure preprocessing. */
import test from 'node:test';
import assert from 'node:assert/strict';
import {validateTraceTypes} from '../web/plotly-view.mjs';

const renderer={version:'test',PlotSchema:{get:()=>({traces:{scatter:{},image:{},scatter3d:{}}})}};
test('default, template and remapped frame types are checked without mutation',()=> {
  const figure={data:[{x:[0],y:[1]},{type:'image',z:[[1]]}],layout:{template:{data:{scatter3d:[{type:'scatter3d'}]}}},frames:[{traces:[1],data:[{source:'data:image/png;base64,AAAA'}]}]};
  const saved=JSON.stringify(figure);validateTraceTypes(figure,renderer);assert.equal(JSON.stringify(figure),saved);
  for(const variant of [
    {data:[{type:'future_trace'}]},
    {data:[],layout:{template:{data:{future_trace:[{}]}}}},
    {data:[{type:'scatter'}],frames:[{data:[{type:'future_trace'}]}]},
    {data:[{type:'constructor'}]},
  ])assert.throws(()=>validateTraceTypes(variant,renderer),/not supported.*Plotly test/);
  for(const index of [-1,999,1.5])assert.throws(()=>validateTraceTypes({data:[{type:'scatter'}],frames:[{traces:[index],data:[{type:'scatter'}]}]},renderer),/outside the saved figure/);
});

test('redraw-disabled title requests pass trace validation for native final application',()=> {
  const frames=[{name:'title',group:'titled',layout:{title:{text:'Next'}}},{name:'data',data:[{x:[1]}]},{name:'child',baseframe:'title',group:'inherited'}];
  const figure=selection=>({data:[{type:'scatter'}],frames,layout:{updatemenus:[{buttons:[{method:'animate',args:[selection,{frame:{redraw:false}}]}]}]}});
  for(const selection of [['title'],'titled',['child'],'inherited',null,[{layout:{title:'Inline'}}]])validateTraceTypes(figure(selection),renderer);
  validateTraceTypes(figure(['data']),renderer);
  const redraw=figure(['title']);redraw.layout.updatemenus[0].buttons[0].args[1].frame.redraw=true;validateTraceTypes(redraw,renderer);
  const slider=figure(['data']);slider.layout.sliders=[{steps:[{method:'animate',args:[['title'],{frame:{redraw:false}}]}]}];validateTraceTypes(slider,renderer);
  const toggle=figure(['data']);toggle.layout.updatemenus[0].buttons[0].args2=[['title'],{frame:{redraw:false}}];validateTraceTypes(toggle,renderer);
  const separate=figure(['data','title']);separate.layout.updatemenus[0].buttons[0].args[1].frame=[{redraw:false},{redraw:true}];validateTraceTypes(separate,renderer);
});
