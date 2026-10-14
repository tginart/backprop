/** Graph replay contracts: keep declared topology and resolve manifest files. */
import test from 'node:test';
import assert from 'node:assert/strict';
import {graphLayout} from '../web/graph-view.mjs';
import {bindArtifactMedia} from '../web/table-explorer.mjs';

test('graph layout preserves branching, cycles, duplicate IDs and disconnected nodes',()=>{
  const nodes=[{id:'a'},{id:'b'},{id:'c'},{id:'d'}];
  const layout=graphLayout({nodes,edges:[['a','b'],['a','c'],['b','d'],['c','d']]});
  assert.deepEqual(layout.nodes.map(node=>node.level),[0,1,1,2]);
  assert.equal(layout.edges.length,4);assert.equal(layout.nodes[1].row,0);assert.equal(layout.nodes[2].row,1);
  const cyclic=graphLayout({nodes,edges:[['a','b'],['b','a']]});assert.equal(cyclic.nodes.length,4);
  const duplicate=graphLayout({nodes:[{id:'a',name:'old'},{id:'a',name:'new'}],edges:[['a','a']]});
  assert.equal(duplicate.edges[0].from.value.name,'new');assert.equal(duplicate.nodes.length,2);
});
test('a module listing never invents dependency edges and malformed edges are visible errors',()=>{
  const graph=graphLayout({nodes:[{id:1},{id:2}],edges:[]});assert.equal(graph.edges.length,0);
  assert.throws(()=>graphLayout({nodes:[{id:1}],edges:[[1,2]]}),/missing node/);
  assert.throws(()=>graphLayout({nodes:[null],edges:[]}),/valid graph/);
  assert.equal(graphLayout({nodes:[],edges:[]}).nodes.length,0);
});
test('artifact graph files resolve only through the selected immutable manifest',()=>{
  const value={_type:'graph',artifact_path:'media/graph.json',path:'https://invalid.test/graph',dataset_rid:'untrusted'};
  const file={path:'verified-graph',dataset_rid:'allowed'};
  assert.equal(bindArtifactMedia(value,new Map([['media/graph.json',file]])).path,'verified-graph');
  assert.match(bindArtifactMedia(value,new Map())._load_error,/absent/);
});
