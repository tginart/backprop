/** Immutable saved viewport snapshots for independent scenes and axis domains. */
import test from 'node:test';
import assert from 'node:assert/strict';
import {plotlyResetPatch} from '../web/plotly-reset.mjs';

test('explicit axes, template defaults and independent cameras remain immutable',()=> {
  const camera={eye:{x:1,y:2,z:3},up:{x:0,y:0,z:1}};
  const layout={xaxis:{range:[0,2]},template:{layout:{yaxis:{range:[1,5]},scene:{camera:{eye:{x:1.5}}}}}};
  const full={xaxis:{range:[.5,1.5]},yaxis:{range:[2,3]},scene:{camera},scene2:{camera:{eye:{x:-1,y:2,z:3}}},polar:{radialaxis:{range:[0,4],autorange:true},angularaxis:{rotation:0}},ternary:{aaxis:{min:0},baxis:{min:0},caxis:{min:0}}};
  const patch=plotlyResetPatch(layout,full),saved=JSON.stringify(patch);
  assert.deepEqual(patch['xaxis.range'],[0,2]);assert.deepEqual(patch['yaxis.range'],[1,5]);assert.equal(patch['scene.camera'].eye.x,1.5);assert.equal(patch['scene2.camera'].eye.x,-1);
  layout.xaxis.range[0]=99;camera.eye.x=99;assert.equal(JSON.stringify(patch),saved);
  const next=plotlyResetPatch({scene:{camera:{eye:{x:2.1}}},xaxis:{autorange:'reversed'}},full,patch);
  assert.equal(next['scene.camera'].eye.x,2.1);assert.equal(next['scene.camera'].eye.y,2);assert.equal(next['scene2.camera'].eye.x,-1);
  assert.equal(next['xaxis.autorange'],'reversed');assert.equal(next['yaxis.autorange'],true);assert(!Object.hasOwn(next,'xaxis.range'));
  assert.deepEqual(next['polar.radialaxis.range'],[0,4]);assert.equal(next['ternary.aaxis.min'],0);
});
