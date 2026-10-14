/** Codec lifecycle and explicit errors without canvas-dependent fixtures. */
import test from 'node:test';
import assert from 'node:assert/strict';
import {decodeMaskPixels} from '../web/mask-pixels.mjs';

test('mask decoder copies exact RGBA bytes and closes both resources',async()=>{
 const previous=globalThis.ImageDecoder,closed=[];
 globalThis.ImageDecoder=class {
  static async isTypeSupported(type){return type==='image/png';}
  constructor(options){assert.equal(options.colorSpaceConversion,'none');assert.equal(options.type,'image/png');}
  async decode(options){assert.deepEqual(options,{frameIndex:0});return {image:{displayWidth:2,displayHeight:1,async copyTo(bytes,options){assert.deepEqual(options,{format:'RGBA',layout:[{offset:0,stride:8}]});bytes.set([0,0,0,255,255,255,255,255]);},close(){closed.push('frame');}}};}
  close(){closed.push('decoder');}
 };
 const url=URL.createObjectURL(new Blob(['fixture'],{type:'image/png'}));
 try {const value=await decodeMaskPixels(url);assert.equal(value.width,2);assert.equal(value.height,1);assert.deepEqual([...value.data],[0,0,0,255,255,255,255,255]);assert.deepEqual(closed,['frame','decoder']);}
 finally {globalThis.ImageDecoder=previous;URL.revokeObjectURL(url);}
});

test('mask decode failures close resources and remain explicit',async()=>{
 const previous=globalThis.ImageDecoder;
 const url=URL.createObjectURL(new Blob(['fixture'],{type:'image/png'}));
 try {
  globalThis.ImageDecoder=undefined;
  await assert.rejects(decodeMaskPixels('https://example.invalid/mask'),/unexpected URL/);
  await assert.rejects(decodeMaskPixels(url),/ImageDecoder support/);
  globalThis.ImageDecoder=class {static async isTypeSupported(){return false;}};
  await assert.rejects(decodeMaskPixels(url),/cannot decode image\/png/);
  for (const failure of ['decode','copy']) {
   const closed=[];
   globalThis.ImageDecoder=class {
    static async isTypeSupported(){return true;}
    async decode(){if(failure==='decode')throw Error('codec failed');return {image:{displayWidth:1,displayHeight:1,async copyTo(){throw Error('copy failed');},close(){closed.push('frame');}}};}
    close(){closed.push('decoder');}
   };
   await assert.rejects(decodeMaskPixels(url),/failed/);
   assert.deepEqual(closed,failure==='decode'?['decoder']:['frame','decoder']);
  }
 } finally {globalThis.ImageDecoder=previous;URL.revokeObjectURL(url);}
});
