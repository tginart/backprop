/** Captured figure resources and controlled provider requests, without I/O. */
import test from 'node:test';import assert from 'node:assert/strict';
import {resourcePolicy,resolvePlotlyResources,usesLegacyMap} from '../web/plotly-resources.mjs';
import {bindArtifactMedia} from '../web/table-explorer.mjs';

test('map policy permits exact HTTPS origins and never credentials or neighboring hosts',()=> {
 const check=resourcePolicy(['https://tiles.example']);assert.equal(check('https://tiles.example/{z}/{x}?token=private'),'https://tiles.example/{z}/{x}?token=private');
 for(const value of ['https://tiles.example.evil.test/tile','http://tiles.example/tile','https://user:pass@tiles.example/tile','javascript:alert(1)','data:application/json,{}'])assert.throws(()=>check(value),/policy|HTTPS/);
 assert(usesLegacyMap({data:[{type:'scattermapbox',subplot:'mapbox2'}]}));assert(!usesLegacyMap({data:[{type:'scattermap'}]}));
});

test('captured resources preserve saved spec and recursively bind style images and GeoJSON',async()=> {
 const spec={data:[{type:'image',source:'https://private.invalid/img'}],layout:{map:{style:'https://private.invalid/style'},images:[{source:'data:image/svg+xml,<svg/>'}]}};
 const resources=[{path:['data',0,'source'],kind:'image',file:{path:'image'}},{path:['layout','map','style'],kind:'json',file:{path:'style'},base_url:'https://tiles.example/styles/main.json',resources:[{path:['sources','features','data'],kind:'json',file:{path:'geo'}},{path:['sources','overlay','url'],kind:'image',file:{path:'image'}}]}];
 const bytes={style:{version:8,sources:{features:{type:'geojson',data:'features.json'},overlay:{type:'image',url:'image.png'},tiles:{type:'raster',tiles:['../tiles/{z}/{x}/{y}.png']}},layers:[]},geo:{type:'FeatureCollection',features:[]}};
 const calls=[],before=JSON.stringify({spec,resources});
 const out=await resolvePlotlyResources({spec,resources},{getMediaUrl:async file=>'blob:'+file.path,mapOrigins:['https://tiles.example'],fetchResource:async(url,options)=>{calls.push([url,options]);return Response.json(bytes[url.slice(5)]);}});
 assert.equal(out.data[0].source,'blob:image');assert.equal(out.layout.map.style.sources.overlay.url,'blob:image');assert.deepEqual(out.layout.map.style.sources.features.data,bytes.geo);
 assert.deepEqual(out.layout.map.style.sources.tiles.tiles,['https://tiles.example/tiles/{z}/{x}/{y}.png']);assert.equal(JSON.stringify({spec,resources}),before);
 assert(calls.every(([,opts])=>opts.credentials==='omit'&&opts.referrerPolicy==='no-referrer'&&opts.redirect==='error'));
});

test('style and TileJSON requests validate every downstream origin before the renderer starts',async()=> {
 const calls=[],spec={data:[{type:'scattermap',lon:[1],lat:[2]}],layout:{map:{style:'https://tiles.example/style.json'}}};
 const opts={mapOrigins:['https://tiles.example'],fetchResource:async(url,options)=> {calls.push([url,options]);return Response.json(url.endsWith('style.json')?{version:8,sources:{raster:{type:'raster',url:'metadata.json'}},sprite:'sprite',glyphs:'fonts/{fontstack}/{range}.pbf',layers:[]}:{tiles:['https://blocked.invalid/{z}/{x}/{y}.png']});}};
 await assert.rejects(resolvePlotlyResources({spec},opts),/blocked.invalid.*hosting policy/);assert.equal(calls.length,2);
 assert(calls.every(([,opts])=>!opts.headers&&opts.credentials==='omit'));
 await assert.rejects(resolvePlotlyResources({spec:{data:[{type:'image',source:'https://private.invalid/picture?secret=123'}]}}),error=>!error.message.includes('secret')&&/not captured/.test(error.message));
 await assert.rejects(resolvePlotlyResources({spec}, {...opts,fetchResource:async()=>new Response('',{status:401})}),/HTTP 401.*token/);
});

test('genuine legacy renderer retains mapbox subplot/style/token values unchanged',async()=> {
 const spec={data:[{type:'scattermapbox',subplot:'mapbox2',lat:[1],lon:[2],marker:{symbol:'airport'}}],layout:{mapbox2:{style:'mapbox://styles/owner/style',accesstoken:'intended-token',zoom:3}}};
 const out=await resolvePlotlyResources({spec},{mapOrigins:['https://api.mapbox.com']});assert.deepEqual(out,spec);
});

test('artifact Plotly resources bind exact immutable manifest entries and external refs remain explicit',()=> {
 const value={_type:'plotly',spec:{data:[]},resources:[{path:['layout','map','style'],kind:'json',file:{artifact_path:'style.json',dataset_rid:'malicious'},resources:[{path:['sources','image','url'],kind:'image',file:{path:'media.png'}}]}]};
 const files=new Map([['style.json',{name:'style.json',dataset_rid:'native',path:'blobs/style'}],['media.png',{name:'media.png',_backprop_external_reference:true,uri:'s3://bucket/image',path:'media.png'}]]);
 const bound=bindArtifactMedia(value,files);assert.equal(bound.resources[0].file.dataset_rid,'native');assert.equal(bound.resources[0].resources[0].file._backprop_external_reference,true);
 assert.match(bindArtifactMedia(value,new Map())._load_error,/absent/);
});


test('template map styles and partial frame image inheritance remain effective',async()=> {
 const style={version:8,sources:{},layers:[]};
 const spec={data:[{type:'scattermap',lon:[0],lat:[0]},{type:'image',z:[[1]]}],layout:{map:{zoom:2},template:{layout:{map:{style}}}},frames:[{traces:[1],data:[{source:'https://private.invalid/frame.png'}]}]};
 await assert.rejects(resolvePlotlyResources({spec}),/not captured/);
 const out=await resolvePlotlyResources({spec,resources:[{path:['frames',0,'data',0,'source'],kind:'image',file:{path:'image'}}]},{getMediaUrl:async()=> 'blob:frame'});
 assert.deepEqual(out.layout.map.style,style);assert.equal(out.frames[0].data[0].source,'blob:frame');assert.equal(out.frames[0].data[0].type,undefined);
});


test('legacy Stamen, Carto and OSM keep their genuine raster styles and exact provider policy',async()=> {
 const styles={'stamen-terrain':['https://tiles.stadiamaps.com'],'stamen-toner':['https://tiles.stadiamaps.com'],'stamen-watercolor':['https://tiles.stadiamaps.com'],'carto-positron':['https://cartodb-basemaps-c.global.ssl.fastly.net'],'carto-darkmatter':['https://cartodb-basemaps-c.global.ssl.fastly.net'],'open-street-map':['https://a.tile.openstreetmap.org','https://b.tile.openstreetmap.org']};
 for(const [style,mapOrigins] of Object.entries(styles)){const spec={data:[{type:'scattermapbox',lon:[0],lat:[0]}],layout:{mapbox:{style}}};const out=await resolvePlotlyResources({spec},{mapOrigins});assert.equal(out.layout.mapbox.style,style);await assert.rejects(resolvePlotlyResources({spec},{mapOrigins:[]}),/hosting policy/);}
 assert.equal((await resolvePlotlyResources({spec:{data:[{type:'scattermapbox'}],layout:{mapbox:{style:'white-bg'}}}},{mapOrigins:[]})).layout.mapbox.style,'white-bg');
});

test('inline args/args2 and slider frames use captured media and the same provider policy',async()=> {
 const source='https://private.invalid/image?secret=123',frame={traces:[1],data:[{source}],layout:{images:[{source}]}},spec={data:[{type:'scatter'},{type:'image',z:[[1]]}],layout:{updatemenus:[{buttons:[{method:'animate',args:[[frame],{}],args2:[frame,{}]}]}],sliders:[{steps:[{method:'animate',args:[[frame],{}]}]}]}};
 const roots=[['layout','updatemenus',0,'buttons',0,'args',0,0],['layout','updatemenus',0,'buttons',0,'args2',0],['layout','sliders',0,'steps',0,'args',0,0]];
 const resources=roots.flatMap(root=>[['data',0,'source'],['layout','images',0,'source']].map(field=>({path:[...root,...field],kind:'image',file:{path:'image'}})));
 await assert.rejects(resolvePlotlyResources({spec}),error=>/not captured/.test(error.message)&&!error.message.includes('secret'));
 const before=JSON.stringify(spec),out=await resolvePlotlyResources({spec,resources},{getMediaUrl:async()=> 'blob:captured'});
 for(const inline of [out.layout.updatemenus[0].buttons[0].args[0][0],out.layout.updatemenus[0].buttons[0].args2[0],out.layout.sliders[0].steps[0].args[0][0]]){assert.equal(inline.data[0].source,'blob:captured');assert.equal(inline.layout.images[0].source,'blob:captured');}
 assert.equal(JSON.stringify(spec),before);
 const legacy={data:[{type:'scatter'}],layout:{updatemenus:[{buttons:[{method:'animate',args:[[{data:[{type:'scattermapbox'}],layout:{mapbox:{style:'white-bg'}}}],{}]}]}]}};assert(usesLegacyMap(legacy));
 const blocked={data:[{type:'scatter'}],layout:{sliders:[{steps:[{method:'animate',args:[[{layout:{map:{style:{version:8,sources:{tile:{type:'raster',tiles:['https://blocked.invalid/{z}/{x}']}}}}}}],{}]}]}]}};
 await assert.rejects(resolvePlotlyResources({spec:blocked}),/blocked.invalid.*hosting policy/);
});


test('map attributions cannot pass adjacent dangerous attributes to renderer HTML', async()=> {
 const hostile='<details open onload="1" ontoggle="globalThis.compromised=true">credit</details>';
 const spec={data:[{type:'scattermap',lon:[1],lat:[2]}],layout:{map:{style:{version:8,sources:{r:{type:'raster',tiles:[],attribution:hostile}},layers:[]},layers:[{sourcetype:'geojson',source:{type:'FeatureCollection',features:[]},sourceattribution:hostile}]}}};
 const out=await resolvePlotlyResources({spec});
 for(const value of [out.layout.map.style.sources.r.attribution,out.layout.map.layers[0].sourceattribution]) {
   assert(!value.includes('<')); assert(value.includes('&lt;details')); assert(value.includes('credit'));
 }
 assert.equal(spec.layout.map.style.sources.r.attribution,hostile);
});
