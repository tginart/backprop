/** Browser-only native redraw normalization; encoding and timing stay separate.
 * Architecture: saved controls -> selected frame lineage -> copied redraw opts.
 * Main caller: focused Node discovery. Actual geometry/timing has browser tests.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import {preparePlotlyAnimations,validateTraceTypes} from '../web/plotly-view.mjs';
const renderer={PlotSchema:{get:()=>({traces:{scatter:{attributes:{y:{anim:true}}},scatter3d:{attributes:{}},carpet:{attributes:{uid:{anim:true}}}}})}};
const settings={mode:'next',direction:'reverse',fromcurrent:true,frame:[{duration:240,redraw:false},{duration:360,redraw:false}],transition:[{duration:120,easing:'quad-in',ordering:'traces first'},{duration:180,easing:'linear',ordering:'layout first'}]};
const control=(selection,options=settings)=>({method:'animate',args:[selection,structuredClone(options)]});

test('final redraw preserves every timing/control field and original source',()=> {
  const source={data:[{type:'scatter'}],frames:[{name:'data',data:[{y:[1]}]},{name:'layout',group:'selected',layout:{annotations:[{text:'Next'}]}},{name:'child',baseframe:'layout',group:'inherited'}],layout:{updatemenus:[{buttons:[control(['layout','data']),control('inherited'),control(null),{...control(['data']),args2:control([{layout:{title:'Inline'}}]).args}]}],sliders:[{steps:[control('selected')]}]}};
  const saved=JSON.stringify(source),{figure,notices}=preparePlotlyAnimations(source,renderer);
  assert.equal(JSON.stringify(source),saved);assert.equal(notices.length,1);
  for(const args of [...figure.layout.updatemenus[0].buttons.flatMap(button=>[button.args,button.args2].filter(Boolean)),figure.layout.sliders[0].steps[0].args]) {
    if(args[0]?.[0]==='data')continue;
    assert.deepEqual(args[1],{...settings,frame:settings.frame.map(frame=>({...frame,redraw:true}))});
  }
});

test('data-only interpolated controls keep redraw=false; nonanimatable families redraw',()=> {
  for(const type of ['scatter','scatter3d','carpet']) {
    const source={data:[{type}],frames:[{name:'next',data:[{y:[1]}]}],layout:{updatemenus:[{buttons:[control(['next'])]}]}};
    const prepared=preparePlotlyAnimations(source,renderer);
    assert.equal(prepared.figure.layout.updatemenus[0].buttons[0].args[1].frame[0].redraw,type!=='scatter');
    assert.equal(prepared.notices.length,type==='scatter'?0:1);
  }
});

test('invalid selected frame names and cyclic/missing bases fail explicitly',()=> {
  for(const frames of [[],[{name:'next',baseframe:'missing'}],[{name:'next',baseframe:'next'}]]) {
    const source={data:[{}],frames,layout:{updatemenus:[{buttons:[control(['next'])]}]}};
    assert.throws(()=>preparePlotlyAnimations(source,renderer),/missing saved frame|contains a cycle/);
  }
});

test('inline trace validation prevents coercion and intentional execute=false is retained',()=> {
 const source={data:[{type:'scatter'}],layout:{updatemenus:[{buttons:[control([{data:[{type:'unknown'}]}]),{...control(['missing']),execute:false},{method:'restyle',args:[{visible:false}]}]}]}};
 assert.throws(()=>validateTraceTypes(source,renderer),/not supported/);
 source.layout.updatemenus[0].buttons[0]=control([{data:[{y:[1]}]}]);
 const prepared=preparePlotlyAnimations(source,renderer);
 assert.deepEqual(prepared.controls.buttons,[[true,false,false]]);
 assert.equal(prepared.figure.layout.updatemenus[0].buttons[1].execute,false);
 assert.equal(prepared.figure.layout.updatemenus[0].buttons[2].execute,undefined);
});
