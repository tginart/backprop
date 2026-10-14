/**
 * Architecture: retained request revisions -> serial native ownership -> retry
 * and disposal. Main caller: focused Node discovery and independent-engine CI.
 * Why this exists: deterministic boundaries expose races that a final chart
 * screenshot alone cannot prove were safely ordered.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import {EventEmitter} from 'node:events';
import {plotlyUpdateQueue,interruptPlotlyAnimation,bindPlotlyFrameSelections} from '../web/plotly-view.mjs';
const deferred=()=>{let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});return{promise,resolve,reject};};
test('unstarted retained requests coalesce to the newest figure',async()=>{
 const applied=[],busy=[],queue=plotlyUpdateQueue('A',{apply:async value=>applied.push(value),report:()=>assert.fail(),busy:value=>busy.push(value)});
 queue.request('B','B');queue.request('C','C');await queue.pending;assert.deepEqual(applied,['C']);assert.equal(queue.busy,false);assert.equal(busy.at(-1),false);assert.equal(queue.request('C','C'),false);
});
test('in-flight ownership finishes before the latest request applies',async()=>{
 const gate=deferred(),events=[],queue=plotlyUpdateQueue('A',{apply:async(value,current)=>{events.push('start '+value);if(value==='B')await gate.promise;events.push('finish '+value+' current='+current());},report:()=>assert.fail()});
 queue.request('B','B');await Promise.resolve();queue.request('C','C');queue.request('D','D');assert.deepEqual(events,['start B']);gate.resolve();await queue.pending;assert.deepEqual(events,['start B','finish B current=false','start D','finish D current=true']);
});
test('the initial figure can become newest again while another request is pending',async()=>{
 const applied=[],queue=plotlyUpdateQueue('A',{apply:async value=>applied.push(value),report:()=>assert.fail()});queue.request('B','B');assert.equal(queue.request('A','A'),true);await queue.pending;assert.deepEqual(applied,['A']);
});
test('failed descriptor is retryable and its actual failure remains visible',async()=>{
 const failure=new Error('actual resource read failure'),reports=[],busy=[];let attempts=0;
 const queue=plotlyUpdateQueue('A',{apply:async()=>{if(++attempts===1)throw failure;},report:(error,retry)=>reports.push({error,retry}),busy:value=>busy.push(value)});
 queue.request('B','B');await queue.pending;assert.equal(reports.length,1);assert.equal(reports[0].error,failure);assert.equal(queue.busy,false);assert.equal(reports[0].retry(),true);await queue.pending;assert.equal(attempts,2);assert.equal(queue.request('B','B'),false);assert.equal(busy.at(-1),false);
});
test('superseded failure cannot replace the latest figure error or retry',async()=>{
 const gate=deferred(),reports=[],applied=[],queue=plotlyUpdateQueue('A',{apply:async(value)=>{if(value==='B')await gate.promise;applied.push(value);},report:error=>reports.push(error)});
 queue.request('B','B');await Promise.resolve();queue.request('C','C');gate.reject(new Error('obsolete failure'));await queue.pending;assert.deepEqual(reports,[]);assert.deepEqual(applied,['C']);
});
test('disposal invalidates pending and in-flight requests without starting newer work',async()=>{
 const gate=deferred(),applied=[],queue=plotlyUpdateQueue('A',{apply:async(value,current)=>{await gate.promise;if(current())applied.push(value);},report:()=>assert.fail()});queue.request('B','B');await Promise.resolve();queue.request('C','C');queue.close();assert.equal(queue.busy,false);assert.equal(queue.request('D','D'),false);gate.resolve();await queue.pending;assert.deepEqual(applied,[]);
});
test('native queue or duration ownership uses the exact zero-duration interruption',async()=>{
 for(const node of [{_transitionData:{_animationRaf:1}},{_transitioningWithDuration:true}]){const calls=[],gate=deferred(),renderer={animate:(graph,frame,options)=>{calls.push({graph,frame,options});return gate.promise;}};const interrupted=interruptPlotlyAnimation(node,renderer);assert.deepEqual(calls,[{graph:node,frame:{data:[],layout:{}},options:{mode:'immediate',frame:{duration:0,redraw:true},transition:{duration:0}}}]);gate.resolve();await interrupted;}
});
test('idle update does not animate and native interruption errors remain failures',async()=>{
 await interruptPlotlyAnimation({}, {animate:()=>assert.fail('idle graph has no animation work')});const error=new TypeError('native failure');await assert.rejects(interruptPlotlyAnimation({_transitioningWithDuration:true},{animate:()=>Promise.reject(error)}),value=>value===error);
});
test('react selections replace old containers rather than merging frame indexes',()=>{
 const node=Object.assign(new EventEmitter(),{layout:{selections:[{x0:1},{x0:2}]},_fullLayout:{selections:[{x0:1},{x0:2}]}}),dispose=bindPlotlyFrameSelections(node,{version:'4.1.1'}),layout={selections:[{x0:7}]},before=structuredClone(layout);
 assert.equal(dispose.stageReact(layout),true);assert.deepEqual(node.layout.selections,[]);assert.deepEqual(layout.selections,[]);node.layout=layout;node.emit('plotly_transitioning');assert.deepEqual(layout,before);dispose();
});
test('react omission clears old root and named-template selections after a no-transition result',()=>{
 const template={layout:{selections:[{name:'old',x0:1}]}},node=Object.assign(new EventEmitter(),{layout:{selections:[{x0:1}],template},_fullLayout:{selections:[{x0:1}]}}),dispose=bindPlotlyFrameSelections(node,{version:'4.1.1'}),layout={title:{text:'replacement'}};
 assert.equal(dispose.stageReact(layout),true);assert.deepEqual(template.layout.selections,[]);node.layout=layout;assert.equal(dispose.restore(),1);assert.deepEqual(layout,{title:{text:'replacement'}});assert.deepEqual(template.layout.selections,[{name:'old',x0:1}]);dispose();
});
test('react template replacement restores both saved input objects and current defaults',()=>{
 const previous={layout:{selections:[{name:'old',x0:1}]}},next={layout:{selections:[{name:'new',x0:7}]}},node=Object.assign(new EventEmitter(),{layout:{template:previous},_fullLayout:{selections:[{x0:1}]}}),dispose=bindPlotlyFrameSelections(node,{version:'4.1.1'}),layout={template:next},before=structuredClone(layout);
 dispose.stageReact(layout);assert.deepEqual(previous.layout.selections,[]);assert.deepEqual(next.layout.selections,[]);node.layout=layout;node.emit('plotly_transitioning');assert.deepEqual(layout,before);assert.deepEqual(previous.layout.selections,[{name:'old',x0:1}]);dispose();
});
