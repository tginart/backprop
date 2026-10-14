/**
 * Architecture: actual JavaScript exception fields plus separate automation
 * message -> exact visible-alert comparison -> strict runtime-error count.
 * Main callers: focused Node checks and independent browser CI discovery.
 * Why this exists: transport prefixes must not change the user's visible error
 * or become a general error allowlist. Browser QA reads native error/rejection events.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import {assertEmptyOptionFailure} from './plotly_animation_options.mjs';

test('actual JavaScript message stays exact when automation includes its name',()=>{
 const error=new TypeError("undefined is not an object (evaluating 'P.duration')");
 const diagnostic={viewer_alert:'Could not animate chart: '+error.message,native_exception:{name:error.name,message:error.message},native_runtime_error:error.name+': '+error.message};
 assertEmptyOptionFailure(diagnostic,[diagnostic.native_runtime_error]);
 const raw=structuredClone(diagnostic);assertEmptyOptionFailure(diagnostic,[diagnostic.native_runtime_error]);assert.deepEqual(diagnostic,raw,'both original messages retained');
});

test('unprefixed automation message uses the same exact JavaScript comparison',()=>{
 const message='cannot read duration from an undefined option';
 const diagnostic={viewer_alert:'Could not animate chart: '+message,native_exception:{name:'TypeError',message},native_runtime_error:message};
 assertEmptyOptionFailure(diagnostic,[message]);
});

test('changed alert, missing native evidence, wrong exception and extra errors fail',()=>{
 const message='native option duration failure',diagnostic={viewer_alert:'Could not animate chart: '+message,native_exception:{name:'TypeError',message},native_runtime_error:'TypeError: '+message};
 assert.throws(()=>assertEmptyOptionFailure({...diagnostic,viewer_alert:'Could not animate chart: different failure'},[diagnostic.native_runtime_error]),/actual native engine exception/);
 assert.throws(()=>assertEmptyOptionFailure({...diagnostic,native_exception:null},[diagnostic.native_runtime_error]),/exception type retained/);
 assert.throws(()=>assertEmptyOptionFailure({...diagnostic,native_exception:{name:'Error',message}},[diagnostic.native_runtime_error]),/exception type retained/);
 assert.throws(()=>assertEmptyOptionFailure({...diagnostic,native_exception:{name:'TypeError',message:''}},[diagnostic.native_runtime_error]),/message retained/);
 assert.throws(()=>assertEmptyOptionFailure(diagnostic,[diagnostic.native_runtime_error,'unexpected runtime failure']),/only the proven/);
 assert.throws(()=>assertEmptyOptionFailure(diagnostic,[]),/only the proven/);
});
