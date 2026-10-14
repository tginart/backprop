/** Native Plotly preparation contract. Geometry is verified with the actual
 * pinned browser asset in browser_plotly_qa.mjs, never a mocked renderer. */
import test from 'node:test';
import assert from 'node:assert/strict';
import {preparePlotly,PLOTLY_SOURCE} from '../web/plotly-view.mjs';

test('Plotly preserves 2D geometry, templates, axes and styling without subset conversion',()=> {
 const spec={data:[{type:'heatmap',z:[[1,2],[3,4]],colorscale:'Viridis'},{type:'bar',orientation:'h',x:[1,2],y:['a','b'],xaxis:'x2',yaxis:'y2'}],layout:{template:{layout:{paper_bgcolor:'pink',font:{color:'red'}}},barmode:'stack',xaxis2:{type:'date',domain:[.6,1]},annotations:[{text:'<b>Saved annotation</b>'}]},frames:[{name:'next',data:[{z:[[4,3],[2,1]]}]}]};
 const before=JSON.stringify(spec),prepared=preparePlotly(spec);
 assert.deepEqual(prepared.problems,[]);assert.deepEqual(prepared.figure.data,spec.data);
 assert.deepEqual(prepared.figure.frames,spec.frames);assert.deepEqual(prepared.figure.layout.template,spec.layout.template);
 assert.equal(prepared.figure.layout.paper_bgcolor,undefined);assert.equal(prepared.figure.layout.font,undefined);
 assert.equal(prepared.figure.layout.xaxis2.type,'date');assert.equal(JSON.stringify(spec),before);
 assert.equal(PLOTLY_SOURCE,'./plotly-strict-4.1.1.min.js');
});

test('native nonfinite values become null gaps in all dimensions without changing source',()=> {
 const number=value=>({_type:'number',value}),spec={data:[{x:[0,1,2,number('-Infinity'),4],y:[1,number('NaN'),3,4,number('Infinity')]},{type:'heatmap',z:[[1,number('NaN')],[number('Infinity'),4]]}]};
 const before=JSON.stringify(spec),out=preparePlotly(spec).figure;
 assert.deepEqual(out.data[0].x,[0,1,2,null,4]);assert.deepEqual(out.data[0].y,[1,null,3,4,null]);
 assert.deepEqual(out.data[1].z,[[1,null],[null,4]]);assert.equal(JSON.stringify(spec),before);
});

test('typed buffers, frames and explicit light colors remain genuine Plotly inputs',()=> {
 const spec={data:[{x:{bdata:'AAEC',dtype:'u1'},y:[1,2,3],line:{color:'red'},hovertemplate:'%{y}<extra></extra>'}],layout:{paper_bgcolor:'#fff',plot_bgcolor:'#fff',font:{color:'#000'},uirevision:'saved'}};
 const {figure}=preparePlotly(spec,'new');assert.deepEqual(figure.data,spec.data);assert.equal(figure.layout.uirevision,'saved');assert.equal(figure.layout.paper_bgcolor,'#fff');
});

test('preparation preserves resource inputs and unrelated source fields for the resource resolver',()=> {
 const spec={data:[{type:'image',source:'https://example.invalid/private.png',customdata:[{source:'dataset provenance'}]}],layout:{images:[{source:'data:image/svg+xml,<svg/>'}],annotations:[{text:'<a href="javascript:alert(1)">read me</a>'}]}};
 const result=preparePlotly(spec);assert.deepEqual(result.problems,[]);
 assert.equal(result.figure.data[0].source,spec.data[0].source);assert.equal(result.figure.data[0].customdata[0].source,'dataset provenance');assert.equal(result.figure.layout.annotations[0].text,'read me');
 assert.deepEqual(preparePlotly({data:[{type:'scattergeo'}]}).problems,[]);assert.deepEqual(preparePlotly({data:[{type:'scatter3d'}]}).problems,[]);
 assert(!Object.hasOwn(preparePlotly(JSON.parse('{"data":[{"__proto__":{"polluted":true}}]}')).figure.data[0],'__proto__'));
});

test('malformed figures fail visibly and defaults preserve explicit layout values',()=> {
 for(const value of [null,{}, {data:[null]}])assert(preparePlotly(value).problems.length);
 const {figure}=preparePlotly({data:[],layout:{margin:{l:10},title:'Saved title',xaxis:{title:'Step'}}});assert.equal(figure.layout.title.text,'Saved title');assert.equal(figure.layout.xaxis.title.text,'Step');assert.deepEqual(figure.layout.margin,{l:10});assert.equal(figure.layout.font.color,'#c5cbd3');
});
