/** Actual Python graph snapshots replay identically after duplicate delivery. */
import test from 'node:test';
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';
import {normalizeEvents} from '../web/model.mjs';
import {graphLayout} from '../web/graph-view.mjs';
import {parseGraphJSON,inspectGraphValue} from '../web/graph-json.mjs';

test('Python JSON non-finite values preserve quoted strings and object keys',()=>{
 const graph=parseGraphJSON(' {"nodes":[{"id":1,"NaN":NaN,"v":[Infinity,-Infinity,"NaN","escaped\\\"NaN"]}],"edges":[],"__proto__":{"safe":true}} ');
 assert(Number.isNaN(graph.nodes[0].NaN));assert.deepEqual(graph.nodes[0].v,[Infinity,-Infinity,'NaN','escaped"NaN']);
 assert.equal(Object.getPrototypeOf(graph),Object.prototype);assert.equal(graph.__proto__.safe,true);assert.equal({}.safe,undefined);
 assert.match(inspectGraphValue(graph),/"Infinity"/);assert.match(inspectGraphValue(graph),/"NaN"/);
 const valid=[null,true,false,0,-1,1.2,1e-20,'unicode ⚗️\n',[1,{},[]],{key:'a\\b"c',NaN:'literal'}];
 for(const value of valid)assert.deepEqual(parseGraphJSON(JSON.stringify(value)),value);
 assert.deepEqual(parseGraphJSON('{"same":1,"same":2}'),{same:2});
});
test('graph file parser rejects malformed JSON and executable expressions',()=>{
 for(const input of ['', 'NaN1','[Infinity,]','{NaN:1}','{"a":NaN,}','[+Infinity]','[undefined]','[01]','[.2]','[1.]','[1e]','[NaN -Infinity]','[NaN] trailing','{"x":"bad\\q"}','"unescaped\nline"','[(()=>1)()]','[NaN/*comment*/]'])assert.throws(()=>parseGraphJSON(input),SyntaxError,input);
 assert.throws(()=>parseGraphJSON(null),TypeError);
});
test('journal restart and graph mutations agree with Python under reordered duplicate delivery',()=>{
 const data=JSON.parse(execFileSync(process.env.BACKPROP_PYTHON||'python3',['-B','tests/graph_replay_fixture.py'],{encoding:'utf8',env:{...process.env,PYTHONPATH:'sdk'}}));
 const result=normalizeEvents([...data.events].reverse().concat(data.events));assert.deepEqual(result.issues,[]);
 const run=result.runs[0];for(const key of ['config','summary','state'])assert.deepEqual(JSON.parse(JSON.stringify(run[key])),data.expected[key]);
 const history=run.history.map(row=>Object.fromEntries(Object.entries(row).filter(([key])=>!['_timestamp','_runtime'].includes(key))));assert.deepEqual(history,data.history);
 const graphs=data.files.map(parseGraphJSON);assert.deepEqual(graphs.map(g=>g.nodes.length),data.node_counts);assert.deepEqual(data.node_counts,[2,3,4]);
 assert.equal(graphLayout(graphs[0]).edges[0].from.value.name,'original');assert.equal(graphLayout(graphs[1]).edges[0].from.value.name,'replacement');
 for(const graph of graphs){assert(Number.isNaN(graph.nodes[0].nan));assert.equal(graph.nodes[0].positive,Infinity);assert.equal(graph.nodes[0].negative,-Infinity);assert.equal(graph.nodes[0].text,'NaN');}
});
