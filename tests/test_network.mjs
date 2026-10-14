import test from 'node:test';
import assert from 'node:assert/strict';
import {withDeadline, bufferedResponse} from '../web/network.mjs';

test('stalled transport rejects and aborts even when the transport ignores cancellation', async () => {
  let signal;
  await assert.rejects(withDeadline(s => {signal=s;return new Promise(()=>{});}, {timeoutMs:10}), {name:'TimeoutError'});
  assert(signal.aborted);
  assert.equal(await withDeadline(async()=>42),42);
});
test('stalled response body is bounded with its request', async () => {
  const response = new Response(new ReadableStream({start(){}}));
  await assert.rejects(withDeadline(()=>bufferedResponse(response),{timeoutMs:10}),{name:'TimeoutError'});
});
test('external cancellation propagates and a pre-cancelled operation never starts', async () => {
  const controller=new AbortController();let calls=0;
  const pending=withDeadline(()=>{calls++;return new Promise(()=>{});},{signal:controller.signal});
  await Promise.resolve();controller.abort();
  await assert.rejects(pending,{name:'AbortError'});
  await assert.rejects(withDeadline(()=>calls++,{signal:controller.signal}),{name:'AbortError'});
  assert.equal(calls,1);
});
test('successful operations remove timers and abort listeners', async () => {
  const controller=new AbortController();let adds=0,removes=0;
  const add=controller.signal.addEventListener.bind(controller.signal),remove=controller.signal.removeEventListener.bind(controller.signal);
  controller.signal.addEventListener=(...args)=>{adds++;return add(...args);};
  controller.signal.removeEventListener=(...args)=>{removes++;return remove(...args);};
  assert.equal(await withDeadline(async()=>7,{signal:controller.signal}),7);
  assert.equal(adds,removes);
});
