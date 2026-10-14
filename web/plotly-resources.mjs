/**
 * Architecture: immutable saved figure -> captured Foundry resources -> explicit
 * provider policy -> renderer-ready copy. Finite images/GeoJSON live in Foundry;
 * map tiles stay dynamic so pan and zoom do not depend on a frozen viewport.
 * Main callers: plotly-view mount/update. No application token or cookies enter
 * provider requests. Original specifications and resource descriptors stay intact.
 */
// CVE-2026-85061: renderer sanitizer skips adjacent attributes. Keep all
// user/provider attribution as literal text before handing it to either renderer.
const attributionText = value => typeof value === 'string' ? value.replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;').replaceAll("'",'&#39;') : value;
const forbidden = new Set(['__proto__','prototype','constructor']);
const safeCopy = value => Array.isArray(value) ? value.map(safeCopy) : value && typeof value==='object' ? Object.fromEntries(Object.entries(value).filter(([key])=>!forbidden.has(key)).map(([key,item])=>[key,safeCopy(item)])) : value;
const maps = name => /^(map|mapbox)\d*$/.test(name);
// Pinned3.6 mapbox/constants.js owns these styles. Keep their actual raster
// providers and semantics instead of substituting current MapLibre styles.
const legacyStyles={
  'white-bg':[], 'open-street-map':['https://a.tile.openstreetmap.org','https://b.tile.openstreetmap.org'],
  'carto-positron':['https://cartodb-basemaps-c.global.ssl.fastly.net'],'carto-darkmatter':['https://cartodb-basemaps-c.global.ssl.fastly.net'],
  'stamen-terrain':['https://tiles.stadiamaps.com'],'stamen-toner':['https://tiles.stadiamaps.com'],'stamen-watercolor':['https://tiles.stadiamaps.com']
};
const mapboxStyles=['basic','streets','outdoors','light','dark','satellite','satellite-streets'];
const builtinStyles = {basic:'voyager',streets:'voyager',outdoors:'voyager',light:'positron',dark:'dark-matter','carto-positron':'positron','carto-darkmatter':'dark-matter','carto-positron-nolabels':'positron-nolabels','carto-darkmatter-nolabels':'dark-matter-nolabels','carto-voyager':'voyager','carto-voyager-nolabels':'voyager-nolabels'};
/** Visit stored and inline native animation frames without expanding names.
 * Main callers: resource resolution, renderer selection and trace validation.
 * Why this exists: animate args/args2 may embed resource-bearing frames instead
 * of referring to figure.frames. Both forms must cross the same policy boundary.
 */
export function animationFrames(spec) {
  const frames=[...(spec?.frames || [])];
  for(const control of [...(spec?.layout?.updatemenus || []).flatMap(menu=>menu.buttons || []),...(spec?.layout?.sliders || []).flatMap(slider=>slider.steps || [])])if(control.method==='animate')for(const args of [control.args,control.args2].filter(Array.isArray)) {
    const selection=args[0];for(const frame of Array.isArray(selection)?selection:[selection])if(frame&&typeof frame==='object'&&!Array.isArray(frame))frames.push(frame);
  }
  return frames;
}
export const usesLegacyMap = spec => [...(spec?.data || []),...animationFrames(spec).flatMap(frame=>frame.data || [])].some(trace=>/^(scatter|choropleth|density)mapbox$/.test(trace?.type));
const site = () => globalThis.location?.origin || 'https://backprop.invalid';

/** Validate hosts, never infer permission from a URL embedded in logged data.
 * Main callers: style, tile, glyph, sprite and uncaptured finite resource reads.
 * Error text includes only an origin, so signed query strings remain private.
 */
export function resourcePolicy(origins=[]) {
  const allowed=new Set(origins);
  return (input,base=site())=> {
    let url;try{url=new URL(input,base);}catch{throw new Error('A map resource has an invalid URL.');}
    if(url.username || url.password || !['https:','http:'].includes(url.protocol))throw new Error('Map resources require an HTTPS URL without embedded credentials.');
    if(url.origin!==site() && (url.protocol!=='https:' || !allowed.has(url.origin)))throw new Error(`Map resource origin ${url.origin} is blocked by this app's hosting policy. The operator can add it to map_resource_origins and redeploy.`);
    return url.href.replace(/%7B/gi,'{').replace(/%7D/gi,'}');
  };
}

function assign(root,path,value) {
  if(!Array.isArray(path)||!path.length||path.some(key=>forbidden.has(key)||typeof key!=='string'&&!Number.isSafeInteger(key)))throw new Error('Captured Plotly resource has an invalid field path.');
  let owner=root;for(const key of path.slice(0,-1)){if(!owner||typeof owner!=='object'||!Object.hasOwn(owner,key))throw new Error('Captured Plotly resource field is absent from its saved figure.');owner=owner[key];}
  if(!owner||typeof owner!=='object'||!Object.hasOwn(owner,path.at(-1)))throw new Error('Captured Plotly resource field is absent from its saved figure.');
  owner[path.at(-1)]=value;
}

/** Materialize finite resources, then validate all dynamic map requests.
 * Main callers: renderer mount/update. Captured JSON styles can contain their
 * own captured resources and a base URL for relative tile paths. Failures stop
 * rendering visibly instead of silently dropping a layer or background image.
 */
export async function resolvePlotlyResources(entry,{getMediaUrl,mapOrigins=[],fetchResource=globalThis.fetch}={}) {
  const spec=safeCopy(entry.spec),check=resourcePolicy(mapOrigins),trustedBlobs=new Set(),bases=new WeakMap();
  async function readJSON(url,isLocal=false) {
    try {
      const response=await fetchResource(url,{credentials:'omit',referrerPolicy:'no-referrer',redirect:isLocal?'error':'error',signal:AbortSignal.timeout(20000)});
      if(!response.ok)throw new Error(`HTTP ${response.status}`);
      const value=await response.json();if(!value||typeof value!=='object'||Array.isArray(value))throw new Error('Expected a JSON object');return safeCopy(value);
    }catch(error){throw new Error(`Could not read ${isLocal?'captured':'map'} JSON (${error.message.includes('HTTP')?error.message:'network, CORS, or invalid JSON'}). Check provider access, token and hosting policy.`);}
  }
  async function captured(root,resources,depth=0) {
    if(depth>16)throw new Error('Captured Plotly resources are too deeply nested.');
    for(const resource of resources || []) {
      if(!['image','json'].includes(resource.kind)||!getMediaUrl)throw new Error('Captured Plotly resources require the Foundry media reader.');
      const url=await getMediaUrl(resource.file);if(typeof url!=='string'||!url.startsWith('blob:'))throw new Error('The Foundry resource reader did not return a local object URL.');
      let value=url;trustedBlobs.add(url);
      if(resource.kind==='json') {value=await readJSON(url,true);await captured(value,resource.resources,depth+1);if(resource.base_url)bases.set(value,resource.base_url);}
      assign(root,resource.path,value);
    }
  }
  await captured(spec,entry.resources);
  const image = async (value,traceSource=false) => {
    if(typeof value!=='string')return value;
    let url=value;
    if(!trustedBlobs.has(value) && !/^data:image\/[a-z\d.+-]+[;,]/i.test(value)) {
      try{url=check(value);}catch{throw new Error('This figure image was not captured in Foundry and its origin is not permitted by the hosting policy. Log the figure again with the current SDK, or configure its resource origin.');}
    }
    const decoded=typeof Image==='undefined'?null:await new Promise((resolve,reject)=> {
      const node=new Image(),timer=setTimeout(()=>{node.src='';reject(new Error('Figure image decoding timed out.'));},20000);
      const finish=error=>{clearTimeout(timer);node.onload=null;node.onerror=null;error?reject(error):resolve(node);};
      node.crossOrigin='anonymous';node.referrerPolicy='no-referrer';node.onload=()=>finish();node.onerror=()=>finish(new Error('Figure image could not load or decode. Check its format, provider access and CORS.'));node.src=url;
    });
    // Plotly image traces accept base64 raster data only. Layout images and
    // map layers accept blob/SVG URLs; keep their original resource form.
    if(traceSource && decoded && !/^data:image\/\w+;base64,/.test(url)) {
      const canvas=document.createElement('canvas');canvas.width=decoded.naturalWidth;canvas.height=decoded.naturalHeight;
      const context=canvas.getContext('2d');if(!context)throw new Error('The browser could not prepare this image trace.');
      context.drawImage(decoded,0,0);return canvas.toDataURL('image/png');
    }
    return url;
  };
  const json = async (value,base) => typeof value==='string' ? readJSON(check(value,base)) : value;
  async function style(value,legacy,base) {
    if(value===undefined)value=legacy?'basic':'basic';
    if(typeof value==='string') {
      if(legacy && Object.hasOwn(legacyStyles,value)) {
        for(const origin of legacyStyles[value])check(origin);
        if((spec.data || []).some(trace=>trace.type?.endsWith('mapbox')&&trace.mode?.includes('text')))check('https://fonts.openmaptiles.org');
        return value;
      }
      if(legacy && (mapboxStyles.includes(value)||value.startsWith('mapbox://'))){check('https://api.mapbox.com');return value;}
      if(value==='white-bg')value={version:8,sources:{},layers:[{id:'background',type:'background',paint:{'background-color':'#ffffff'}}],...((spec.data || []).some(trace=>trace.mode?.includes('text'))?{glyphs:'https://fonts.openmaptiles.org/{fontstack}/{range}.pbf'}:{})};
      else if(value==='open-street-map')value={version:8,sources:{osm:{type:'raster',tiles:['https://tile.openstreetmap.org/{z}/{x}/{y}.png'],tileSize:256,attribution:'© OpenStreetMap contributors'}},layers:[{id:'osm',type:'raster',source:'osm'}],glyphs:'https://fonts.openmaptiles.org/{fontstack}/{range}.pbf'};
      else if(builtinStyles[value])value=`https://basemaps.cartocdn.com/gl/${builtinStyles[value]}-gl-style/style.json`;
      else if(value==='satellite'||value==='satellite-streets')value=new URL(value==='satellite'?'./plotly-map-sat.json':'./plotly-map-hybrid.json',import.meta.url).href;
      if(typeof value==='string') {base=check(value,base);value=await readJSON(base);}
    }
    if(!value||typeof value!=='object')throw new Error('Map style must be a named style, JSON style or URL.');
    base=bases.get(value)||base;
    if(value.glyphs)value.glyphs=check(value.glyphs,base);
    if(typeof value.sprite==='string')value.sprite=check(value.sprite,base);
    else if(Array.isArray(value.sprite))for(const sprite of value.sprite)if(sprite?.url)sprite.url=check(sprite.url,base);
    for(const source of Object.values(value.sources || {})) {
      if(!source||typeof source!=='object')continue;
      if(source.type==='geojson')source.data=await json(source.data,base);
      if(source.type==='image')source.url=await image(source.url);
      if(source.type==='video')source.urls=(source.urls || []).map(url=>check(url,base));
      if(source.url && ['vector','raster','raster-dem'].includes(source.type)) {
        if(legacy && source.url.startsWith('mapbox://')){check('https://api.mapbox.com');continue;}
        const url=check(source.url,base),tileJSON=await readJSON(url);
        if(!Array.isArray(tileJSON.tiles))throw new Error('Map TileJSON has no tile URL list.');
        const merged={...tileJSON,...source,tiles:source.tiles || tileJSON.tiles};delete merged.url;Object.assign(source,merged);delete source.url;
        source.tiles=source.tiles.map(tile=>check(tile,url));
      }else if(Array.isArray(source.tiles))source.tiles=source.tiles.map(url=>check(url,base));
    }
    for (const source of Object.values(value.sources || {})) if(source && Object.hasOwn(source,'attribution')) source.attribution=attributionText(source.attribution);
    return value;
  }
  async function traces(values,type) {for(const [index,trace] of (values || []).entries()) {
    if((trace.type || (Array.isArray(type)?type[index]:type))==='image' && trace.source)trace.source=await image(trace.source,true);
    if(trace.geojson)trace.geojson=await json(trace.geojson);
  }}
  async function layout(value,defaults={}) {
    if(!value||typeof value!=='object')return;
    for(const item of value.images || [])if(item?.source)item.source=await image(item.source);
    for(const [name,subplot] of Object.entries(value))if(maps(name)&&subplot&&typeof subplot==='object') {
      subplot.style=await style(subplot.style===undefined?defaults[name]?.style:subplot.style,name.startsWith('mapbox'));
      for(const layer of subplot.layers || []) {
        if(Object.hasOwn(layer,'sourceattribution')) layer.sourceattribution=attributionText(layer.sourceattribution);
        const kind=layer.sourcetype || 'geojson';
        if(kind==='geojson')layer.source=await json(layer.source);
        else if(kind==='image')layer.source=await image(layer.source);
        else if(typeof layer.source==='string')layer.source=check(layer.source);
        else if(Array.isArray(layer.source))layer.source=layer.source.map(url=>check(url));
      }
    }
  }
  if(spec.layout?.template){await layout(spec.layout.template.layout);for(const [type,values] of Object.entries(spec.layout.template.data || {}))await traces(values,type);}
  await traces(spec.data);await layout(spec.layout,spec.layout?.template?.layout);
  const frames=animationFrames(spec);
  for(const frame of frames){await traces(frame.data,(frame.data || []).map((trace,index)=>spec.data?.[frame.traces?.[index] ?? index]?.type));await layout(frame.layout,spec.layout);}
  // Plotly creates default map subplots even when layout omitted them. Resolve
  // those defaults through the same policy instead of bypassing validation.
  for(const trace of [...(spec.data || []),...frames.flatMap(frame=>frame.data || [])])if(/^(scatter|choropleth|density)map(box)?$/.test(trace.type)) {
    const name=trace.subplot || (trace.type.endsWith('mapbox')?'mapbox':'map');spec.layout ??={};
    if(!spec.layout[name]&&!spec.layout.template?.layout?.[name])spec.layout[name]={style:await style(undefined,name.startsWith('mapbox'))};
  }
  return spec;
}
