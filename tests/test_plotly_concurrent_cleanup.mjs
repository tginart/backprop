/**
 * Architecture: exact native ownership boundaries -> synchronous idle purge
 * or awaited pending cleanup -> retained real GPU negatives. Main callers:
 * focused Node checks and release discovery. Why this exists: a settled promise
 * must not postpone idle teardown, while pending renderer callbacks must finish
 * before purge. Error observation does not convert failed native work to success.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {plotlyNativeWork} from '../web/plotly-view.mjs';
import {classifyPlotlyGpu} from './plotly_animation_gpu.mjs';
const deferred=()=>{let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});return{promise,resolve,reject};};

test('initial and settled native work purge synchronously in the caller stage',async()=>{
 const owner=plotlyNativeWork(),calls=[];let phase='teardown';
 const release=()=>{calls.push(phase);return Promise.resolve();};
 const initial=owner.release(release);assert.deepEqual(calls,['teardown']);await initial;
 await owner.track(Promise.resolve());assert.equal(owner.pending,false);
 const settled=owner.release(release);phase='render';assert.deepEqual(calls,['teardown','teardown']);await settled;
});
test('pending native work keeps cleanup awaited until its actual settlement',async()=>{
 const owner=plotlyNativeWork(),gate=deferred(),calls=[];const native=owner.track(gate.promise);
 const cleanup=owner.release(()=>calls.push('purge'));assert.deepEqual(calls,[]);assert.equal(owner.pending,true);
 gate.resolve();await native;await cleanup;assert.deepEqual(calls,['purge']);assert.equal(owner.pending,false);
});
test('newer ownership cannot be cleared by an earlier settled operation',async()=>{
 const owner=plotlyNativeWork(),first=deferred(),second=deferred();owner.track(first.promise);const newer=owner.track(second.promise);first.resolve();await first.promise;
 assert.equal(owner.current,newer);assert.equal(owner.pending,true);const calls=[],cleanup=owner.release(()=>calls.push('purge'));assert.deepEqual(calls,[]);second.resolve();await cleanup;assert.deepEqual(calls,['purge']);assert.equal(owner.pending,false);
});
test('native rejection remains the exact caller failure and still releases ownership',async()=>{
 const owner=plotlyNativeWork(),gate=deferred(),failure=new TypeError('actual native failure'),calls=[];
 const native=owner.track(gate.promise),rejected=assert.rejects(native,error=>error===failure),cleanup=owner.release(()=>calls.push('purge'));
 gate.reject(failure);await rejected;await cleanup;assert.deepEqual(calls,['purge']);assert.equal(owner.pending,false);
 const next=await owner.current.then(()=>7);assert.equal(next,7,'next work can retry after a reported rejection');
});
test('pending cleanup rejection and idle synchronous cleanup throw remain observable',async()=>{
 const owner=plotlyNativeWork(),gate=deferred(),failure=new TypeError('actual interruption failure');owner.track(gate.promise);
 const cleanup=owner.release(()=>Promise.reject(failure)),rejected=assert.rejects(cleanup,error=>error===failure);gate.resolve();await rejected;
 assert.throws(()=>owner.release(()=>{throw failure;}),error=>error===failure);
});
