/**
 * Architecture: immutable saved layout -> resolved subplot defaults -> reset
 * relayout patch. Main callers: Plotly mount and retained specification updates.
 * Why this exists: Plotly mutates the layout objects it receives, so reading
 * those objects after zoom can accidentally reset to the user's current view.
 * Incoming saved cameras belong to the new value; retained interactive cameras
 * remain temporary viewer state. Each scene/subplot keeps an independent reset.
 */
const copy=value=>value===undefined?undefined:JSON.parse(JSON.stringify(value));
function merge(base,value) {
  if(!value||typeof value!=='object'||Array.isArray(value))return copy(value);
  const result=base&&typeof base==='object'&&!Array.isArray(base)?copy(base):{};
  for(const [key,item] of Object.entries(value))result[key]=item&&typeof item==='object'&&!Array.isArray(item)?merge(result[key],item):copy(item);
  return result;
}

/** Capture saved axes/cameras without overwriting them with interactive values.
 * Main callers: mount after newPlot and update after react. Explicit incoming
 * viewport fields override the earlier saved baseline. Unspecified fields keep
 * their baseline, while newly introduced subplots use resolved renderer defaults.
 */
export function plotlyResetPatch(layout,fullLayout,previous={}) {
  const result={};
  for(const [name,subplot] of Object.entries(fullLayout)) {
    const saved=merge(layout.template?.layout?.[name] || {},layout[name] || {});
    if(/^[xy]axis\d*$/.test(name)) {
      if(Array.isArray(saved.range))result[`${name}.range`]=copy(saved.range);
      result[`${name}.autorange`]=Object.hasOwn(saved,'autorange')?saved.autorange:Array.isArray(saved.range)?false:true;
    }
    const fields=/^(map|mapbox)\d*$/.test(name)?['center','zoom','bearing','pitch']:/^geo\d*$/.test(name)?['center','projection']:/^scene\d*$/.test(name)?['camera']:/^polar\d*$/.test(name)?['radialaxis.range','radialaxis.autorange','angularaxis.rotation']:/^ternary\d*$/.test(name)?['aaxis.min','baxis.min','caxis.min']:[];
    for(const field of fields) {
      const read=object=>field.split('.').reduce((value,key)=>value?.[key],object);
      const key=`${name}.${field}`,base=Object.hasOwn(previous,key)?previous[key]:read(subplot),declared=read(saved);
      const value=declared!==undefined?merge(base,declared):copy(base);
      if(value!==undefined)result[key]=value;
    }
  }
  return result;
}
