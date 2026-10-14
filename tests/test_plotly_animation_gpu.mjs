/**
 * Architecture: exact Firefox warning text + independent primitive lifecycle
 * evidence -> real classifier -> accepted notices or explicit GPU failures.
 * Main caller: focused Node tests and ordinary test discovery. These negative
 * cases prevent broad text matching from concealing shader errors or loss of
 * an active context, including one that recovered before the final snapshot.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import {classifyPlotlyGpu} from './plotly_animation_gpu.mjs';

const warning=text=>({type:'warning',text}),firefox=text=>warning(`[JavaScript Warning: "${text}"]`);
const upload='WebGL warning: texImage: Alpha-premult and y-flip are deprecated for non-DOM-Element uploads.';
const portability='WebGL warning: checkFramebufferStatus: Using format enabled by implicitly enabled extension: WEBGL_color_buffer_float. For maximal portability enable it explicitly.';
const debug='WEBGL_debug_renderer_info is deprecated in Firefox and will be removed. Please use RENDERER.';
const cap='After reporting 32, no further warnings will be reported for this WebGL context.';
const context=phase=>({id:0,collected:true,connected:false,debug_requests:phase==='probe'?[{phase,time:0}]:[],loss_requests:[{phase,time:1,connected:phase==='teardown'}],losses:[{time:2,connected:false,request:0}]});
const evidence=(...contexts)=>({snapshots:[],final:contexts});

test('exact probe/teardown notices require observed requests and detached loss events',()=> {
 const probe=context('probe'),map={...context('teardown'),id:1};
 const result=classifyPlotlyGpu([warning(`[JavaScript Warning: "${debug}" {file: "debugger eval code line 311 > eval" line: 1}]`),firefox('WebGL context was lost.'),warning('[JavaScript Warning: "WebGL context was lost." {file: "http://127.0.0.1:35133/plotly-strict-3.6.0.min.js" line: 3214}]'),firefox(upload),firefox(portability)],evidence(probe,map));
 assert.equal(result.passed,true);assert.equal(result.expected_loss_events,2);assert.equal(result.classified.length,5);
});

test('driver and loss notices cannot borrow unrelated or absent lifecycle evidence',()=> {
 assert.equal(classifyPlotlyGpu([firefox(debug)],evidence(context('teardown'))).passed,false);
 assert.equal(classifyPlotlyGpu([firefox('WebGL context was lost.')],evidence()).passed,false);
 assert.equal(classifyPlotlyGpu([firefox('WebGL context was lost.')],evidence(context('render'))).passed,false);
 assert.equal(classifyPlotlyGpu([firefox('WebGL context was lost.'),firefox('WebGL context was lost.')],evidence(context('probe'))).passed,false);
});

test('restored active loss and a collected connected context fail independently of console text',()=> {
 const active={...context('teardown'),collected:false,lost:false,losses:[{time:2,connected:true,request:0}]};
 assert(classifyPlotlyGpu([],evidence(active)).errors.some(error=>error.kind==='active_context_loss_event'));
 assert(classifyPlotlyGpu([],{snapshots:[{case:'drawn',contexts:[{id:1,connected:true,lost:true}]}],final:[]}).errors.some(error=>error.kind==='active_context_lost'));
 assert(classifyPlotlyGpu([],evidence({id:1,collected:true,connected:true})).errors.some(error=>error.kind==='active_context_collected'));
});

test('context pressure, shader/framebuffer failures and GPU errors are always failures',()=> {
 for(const text of ['There are too many active WebGL contexts on this page, the oldest context will be lost.','WebGL shader compilation failed.','WebGL warning: checkFramebufferStatus: FRAMEBUFFER_INCOMPLETE_ATTACHMENT','WebGL: INVALID_OPERATION: loseContext: context already lost','Unknown WebGL renderer warning'])assert.equal(classifyPlotlyGpu([warning(text)],evidence(context('probe'))).passed,false,text);
 assert.equal(classifyPlotlyGpu([{type:'error',text:upload}],evidence()).passed,false);
 assert.equal(classifyPlotlyGpu([firefox(portability+' Framebuffer incomplete.')],evidence()).passed,false);
});

test('Firefox warning limit is retained only with observed repeated exact native notices',()=> {
 const messages=[...Array.from({length:32},()=>firefox(upload)),firefox(cap)];
 assert.equal(classifyPlotlyGpu(messages,evidence()).passed,true);
 assert.equal(classifyPlotlyGpu([firefox(cap)],evidence()).passed,false);
 assert.equal(classifyPlotlyGpu([...messages,warning('WebGL shader failed')],evidence()).passed,false);
});
