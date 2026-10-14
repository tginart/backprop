/**
 * Architecture: native frame container rules -> staged prepare/draw boundaries
 * -> restored source inputs and listener cleanup. Main caller: Node discovery.
 * Why this exists: final data assertions miss null/partial frame inheritance
 * and disposal can otherwise leave temporary browser inputs in a later update.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import {EventEmitter} from 'node:events';
import {frameSelections,bindPlotlyFrameSelections,preparePlotlyAnimations,disposePlotlyGraph} from '../web/plotly-view.mjs';
const original=[{x0:1,x1:4,y0:1,y1:4,line:{color:'#e83fe8',width:1}},{x0:5,x1:7,line:{color:'#00e8e8',width:2}}];
test('selection frame containers merge by index, with explicit null and empty semantics',()=>{
 const before=structuredClone(original);
 assert.deepEqual(frameSelections(original,{selections:[{x0:2,line:{width:5}}]}),[{...original[0],x0:2,line:{color:'#e83fe8',width:5}},original[1]]);
 assert.deepEqual(frameSelections(original,{selections:[]}),original);
 assert.deepEqual(frameSelections(original,{selections:[null]}),[null,original[1]]);
 assert.equal(frameSelections(original,{selections:null}),null);
 assert.deepEqual(frameSelections(original,{}),original);assert.deepEqual(original,before);
});
test('flattened selection paths resolve before native container merge',()=>{
 const result=frameSelections(original,{'selections[1].x0':6,'selections[0].line.width':7});
 assert.deepEqual(result,[{...original[0],line:{color:'#e83fe8',width:7}},{...original[1],x0:6}]);
 assert.equal(frameSelections(undefined,{}),undefined);
});
const graph=()=>Object.assign(new EventEmitter(),{layout:{selections:structuredClone(original)},_fullLayout:{selections:structuredClone(original)}});
test('native prepare temporarily clears selection inputs and transitioning restores exact patches',()=>{
 const node=graph(),dispose=bindPlotlyFrameSelections(node,{version:'4.1.1'}),frame={layout:{'selections[0].line.width':7,xaxis:{range:[0,8]}}},before=structuredClone(frame);
 node.emit('plotly_animatingframe',{frame});assert.deepEqual(node.layout.selections,[]);assert.deepEqual(frame.layout.selections,[]);
 node.emit('plotly_transitioning');assert.equal(node.layout.selections[0].line.width,7);assert.deepEqual(frame,before);
 dispose();assert.equal(node.eventNames().length,0);
});
test('interruption/completion do not restore before pending prepare reaches drawing',()=>{
 for(const finish of ['plotly_transitioninterrupted','plotly_animated','dispose']){
  const node=graph(),dispose=bindPlotlyFrameSelections(node,{version:'4.1.1'}),frame={layout:{selections:[{x0:2}]}};
  node.emit('plotly_animatingframe',{frame});if(finish==='dispose')dispose();else{node.emit(finish);assert.deepEqual(node.layout.selections,[]);node.emit('plotly_transitioning');}
  assert.equal(node.layout.selections[0].x0,2);assert.deepEqual(frame.layout.selections,[{x0:2}]);dispose();assert.equal(node.eventNames().length,0);
 }
});
test('overlapping zero-duration prepares keep selections out until both reach drawing',()=>{
 const node=graph(),dispose=bindPlotlyFrameSelections(node,{version:'4.1.1'}),first={layout:{selections:[{x0:2}]}},second={layout:{selections:[{line:{width:7}}]}};
 node.emit('plotly_animatingframe',{frame:first});node.emit('plotly_animatingframe',{frame:second});
 node.emit('plotly_transitioning');assert.deepEqual(node.layout.selections,[]);assert.deepEqual(first.layout.selections,[{x0:2}]);
 node.emit('plotly_transitioning');assert.equal(node.layout.selections[0].x0,2);assert.equal(node.layout.selections[0].line.width,7);assert.deepEqual(second.layout.selections,[{line:{width:7}}]);dispose();assert.equal(node.eventNames().length,0);
});
test('caught failure or retained update restores every pending patch/template before reuse',()=>{
 const node=graph();node.layout.template={layout:{selections:[{...original[0],name:'saved'}]}};
 const before=structuredClone(node.layout.template),dispose=bindPlotlyFrameSelections(node,{version:'4.1.1'}),first={layout:{selections:[{x0:2}]}},second={layout:{selections:[{line:{width:7}}]}};
 node.emit('plotly_animatingframe',{frame:first});node.emit('plotly_animatingframe',{frame:second});assert.equal(dispose.restore(),2);
 assert.equal(node.layout.selections[0].x0,2);assert.equal(node.layout.selections[0].line.width,7);assert.deepEqual(node.layout.template,before);
 assert.deepEqual(first.layout.selections,[{x0:2}]);assert.deepEqual(second.layout.selections,[{line:{width:7}}]);assert.equal(dispose.restore(),0);
 const next={layout:{selections:null}};node.emit('plotly_animatingframe',{frame:next});node.emit('plotly_transitioning');assert.equal(node.layout.selections,null);dispose();assert.equal(node.eventNames().length,0);
});
test('selection-free and retained legacy graphs keep their original path',()=>{
 const node=graph();node.layout.selections=[];node._fullLayout.selections=[];
 const dispose=bindPlotlyFrameSelections(node,{version:'4.1.1'}),frame={data:[{y:[3]}],layout:{xaxis:{range:[0,8]}}},before=structuredClone(frame);
 node.emit('plotly_animatingframe',{frame});assert.deepEqual(frame,before);dispose();
 const legacy=graph(),remove=bindPlotlyFrameSelections(legacy,{version:'3.6.0'});assert.equal(legacy.eventNames().length,0);remove();
});
test('named template selections are staged too, then restored with their original input',()=>{
 const node=graph();delete node.layout.selections;node.layout.template={layout:{selections:[{...original[0],name:'saved'}]}};
 const before=structuredClone(node.layout.template),dispose=bindPlotlyFrameSelections(node,{version:'4.1.1'}),frame={layout:{selections:[{templateitemname:'saved',x0:2}]}};
 node.emit('plotly_animatingframe',{frame});assert.deepEqual(node.layout.template.layout.selections,[]);
 node.emit('plotly_transitioning');assert.deepEqual(node.layout.template,before);assert.equal(node.layout.selections[0].x0,2);dispose();assert.equal(node.eventNames().length,0);
});
test('frame template introduction and removal preserve exact inputs at the draw boundary',()=>{
 const node=graph(),dispose=bindPlotlyFrameSelections(node,{version:'4.1.1'}),saved=[{...original[0],name:'new-template'}],frame={layout:{template:{layout:{selections:structuredClone(saved)}}}};
 node.emit('plotly_animatingframe',{frame});node.layout.template=structuredClone(frame.layout.template);
 node.emit('plotly_transitioning');assert.deepEqual(node.layout.template.layout.selections,saved);assert.deepEqual(frame.layout.template.layout.selections,saved);
 const previous=node.layout.template,remove={layout:{template:null}};node.emit('plotly_animatingframe',{frame:remove});node.layout.template=null;node.emit('plotly_transitioning');assert.equal(node.layout.template,null);assert.deepEqual(previous.layout.selections,saved);dispose();assert.equal(node.eventNames().length,0);
});
test('only selection-aware native controls require the final selection redraw',()=>{
 const figure={data:[{type:'scatter'}],layout:{selections:original,updatemenus:[{buttons:[{method:'animate',args:[['next'],{frame:{redraw:false,duration:40},transition:{duration:20}}]}]}]},frames:[{name:'next',data:[{y:[3]}]}]},before=structuredClone(figure);
 const renderer={version:'4.1.1',PlotSchema:{get:()=>({traces:{scatter:{attributes:{y:{anim:true}}}}})}};
 const prepared=preparePlotlyAnimations(figure,renderer);assert.equal(prepared.figure.layout.updatemenus[0].buttons[0].args[1].frame.redraw,true);assert.deepEqual(figure,before);
 const plain=structuredClone(figure);delete plain.layout.selections;assert.equal(preparePlotlyAnimations(plain,renderer).figure.layout.updatemenus[0].buttons[0].args[1].frame.redraw,false);
});
test('active pinned disposal keeps renderer state until native interruption completes',async()=>{
 const node={_transitioningWithDuration:true},calls=[];let finish;
 const renderer={version:'4.1.1',animate:(graph,frame,options)=>{assert.equal(graph,node);assert.deepEqual(frame,{data:[],layout:{}});assert.deepEqual(options,{mode:'immediate',frame:{duration:0,redraw:true},transition:{duration:0}});calls.push('interrupt');return new Promise(resolve=>finish=resolve);},purge:()=>calls.push('purge')};
 const disposed=disposePlotlyGraph(node,renderer,()=>calls.push('restore/remove selections'));
 assert.deepEqual(calls,['interrupt']);finish();await disposed;assert.deepEqual(calls,['interrupt','restore/remove selections','purge']);
});
test('inactive/legacy disposal remains immediate and does not animate',async()=>{
 for(const [version,active] of [['4.1.1',false],['3.6.0',true]]){const calls=[],renderer={version,animate:()=>assert.fail('inactive/legacy must not animate'),purge:()=>calls.push('purge')};const disposed=disposePlotlyGraph({_transitioningWithDuration:active},renderer,()=>calls.push('restore'));assert.deepEqual(calls,['restore','purge']);await disposed;}
});
test('pending native prepare waits for interruption before restoring staged inputs and purging',async()=>{
 const node=graph(),remove=bindPlotlyFrameSelections(node,{version:'4.1.1'}),frame={layout:{selections:[{x0:2}]}};
 node._transitionData={_animationRaf:1};node.emit('plotly_animatingframe',{frame});assert.deepEqual(node.layout.selections,[]);
 const calls=[];let finish;const renderer={version:'4.1.1',animate:()=>{calls.push('interrupt');return new Promise(resolve=>finish=resolve);},purge:()=>calls.push('purge')};
 const disposed=disposePlotlyGraph(node,renderer,remove);assert.deepEqual(calls,['interrupt']);assert.deepEqual(node.layout.selections,[]);
 finish();await disposed;assert.equal(node.layout.selections[0].x0,2);assert.deepEqual(frame.layout.selections,[{x0:2}]);assert.equal(node.eventNames().length,0);assert.deepEqual(calls,['interrupt','purge']);
});
test('failed disposal still restores/purges and rejects the actual native error',async()=>{
 for(const synchronous of [false,true]){const error=new TypeError('actual native interruption failure'),calls=[],renderer={version:'4.1.1',animate:()=>{if(synchronous)throw error;return Promise.reject(error);},purge:()=>calls.push('purge')};await assert.rejects(disposePlotlyGraph({_transitioningWithDuration:true},renderer,()=>calls.push('restore')),actual=>actual===error);assert.deepEqual(calls,['restore','purge']);}
});
