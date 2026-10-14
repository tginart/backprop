/** Grouped source slots -> logical rows -> corrected summary roots.
 * Main callers: normalizeEvents. Mirrors tensorboard_summary.py and the numeric
 * rules in metrics.py. Source revisions replace a logical sample in its first
 * occupied slot. Physical writer events and unrelated history keep their order;
 * stale automatic snapshots cannot undo explicit summary updates or deletions.
 */
const plain = value => value && typeof value === 'object' && !Array.isArray(value) && !('_type' in value);
const clone = value => structuredClone(value);
const textOrder = (a,b) => {
  const x=Array.from(a), y=Array.from(b);
  for(let i=0;i<Math.min(x.length,y.length);i++) if(x[i]!==y[i]) return x[i].codePointAt(0)-y[i].codePointAt(0);
  return x.length-y.length;
};

export function orderGroupRows(rows, metrics=row=>row) {
  const slots=new Map();
  rows.forEach((row,index)=>{
    const position=metrics(row)._tensorboard_group_position;
    if(position) {if(!slots.has(position[0])) slots.set(position[0],[]);slots.get(position[0]).push(index);}
  });
  for(const indices of slots.values()) {
    const ordered=indices.map(index=>rows[index]).sort((a,b)=>{
      const x=metrics(a)._tensorboard_group_position,y=metrics(b)._tensorboard_group_position;
      return textOrder(x[1],y[1]) || x[2]-y[2];
    });
    indices.forEach((index,offset)=>{rows[index]=ordered[offset];});
  }
}

function* leaves(mapping,prefix=[]) {
  for(const [key,value] of Object.entries(mapping)) {
    const path=[...prefix,key];
    if(plain(value)) yield* leaves(value,path); else yield [path,value];
  }
}
function setPath(mapping,path,value) {
  let target=mapping;
  for(const key of path.slice(0,-1)) {
    if(!Object.hasOwn(target,key) || !plain(target[key])) target[key]=Object.create(null);
    target=target[key];
  }
  target[path.at(-1)]=clone(value);
}
function deletePath(mapping,path) {
  let target=mapping;
  for(const key of path.slice(0,-1)) {
    if(!Object.hasOwn(target,key) || !plain(target[key])) return;
    target=target[key];
  }
  delete target[path.at(-1)];
}
const matches=(name,key)=>name===key || name.endsWith('*') && key.startsWith(name.slice(0,-1));
const numeric=value=>typeof value==='number' ? value
  : value?._type==='number' && ['NaN','Infinity','-Infinity'].includes(value.value) ? Number(value.value) : null;

/** Small replay-only mirror of MetricReducer; raw nested paths stay intact.
 * Why this exists: recomputing just last values would silently corrupt mean,
 * min/max, explicit updates and summary='none' when imports are deduplicated.
 */
class SummaryReducer {
  constructor() {this.definitions=new Map();this.stats=new Map();this.explicit=new Set();}
  definition(key) {
    return Object.assign(Object.create(null),...Array.from(this.definitions).filter(([name])=>matches(name,key))
      .sort(([a],[b])=>Number(a===key)-Number(b===key) || a.length-b.length).map(([,value])=>value));
  }
  define(payload) {
    this.definitions.set(payload.name,payload);
    for(const encoded of this.explicit) if(matches(payload.name,JSON.parse(encoded).join('.'))) this.explicit.delete(encoded);
  }
  update(values,deleted) {
    for(const [path] of leaves(values)) this.explicit.add(JSON.stringify(path));
    for(const key of deleted) {
      for(const path of this.explicit) if(JSON.parse(path)[0]===key) this.explicit.delete(path);
      for(const [path,stats] of this.stats) if(JSON.parse(path)[0]===key) stats.resetFirst=true;
    }
  }
  apply(metrics,summary) {
    for(const [path,value] of leaves(metrics)) {
      const identity=JSON.stringify(path),ops=(this.definition(path.join('.')).summary || 'copy').split(',');
      if(ops.includes('none')) {deletePath(summary,path);continue;}
      if(!this.stats.has(identity)) this.stats.set(identity,{first:value,last:value,count:0,sum:0,min:0,max:0});
      const stats=this.stats.get(identity);
      if(stats.resetFirst) {stats.first=value;delete stats.resetFirst;}
      stats.last=value;
      const number=numeric(value);
      if(number!==null) {
        stats.min=stats.count ? Math.min(stats.min,number) : number;
        stats.max=stats.count ? Math.max(stats.max,number) : number;
        stats.sum+=number;stats.count++;
      }
      let result=value;
      if(!this.explicit.has(identity) && !(ops.length===1 && ops[0]==='copy')) {
        result=Object.fromEntries(ops.filter(op=>op!=='copy').map(op=>[op,op==='mean'?stats.sum/stats.count:stats[op]]));
        if(!Object.keys(result).length) result=value;
      }
      setPath(summary,path,result);
    }
  }
}

export function groupedSummaries(events, imported, runKey) {
  const timelines=new Map(),roots=new Map(),seen=new Set();
  for(const event of events) {
    if(!event.run_id || seen.has(event.event_id)) continue;
    seen.add(event.event_id);
    const key=runKey(event);
    if(!timelines.has(key)) timelines.set(key,[]);
    if(event.type==='history') {
      for(const metrics of imported.rows(event)) {
        timelines.get(key).push({event,metrics});
        if(metrics._tensorboard_group_position) {
          if(!roots.has(key)) roots.set(key,new Set());
          for(const name of Object.keys(metrics)) if(!name.startsWith('_tensorboard')) roots.get(key).add(name);
        }
      }
    } else timelines.get(key).push({event,metrics:{}});
  }
  const results=new Map();
  for(const [key,owned] of roots) {
    const timeline=timelines.get(key);
    orderGroupRows(timeline,item=>item.metrics);
    let actions=[];
    for(const item of timeline) {
      const {event}=item,p=event.payload;
      if(event.type==='summary' && !p.automatic) for(const name of [...Object.keys(p.values || {}),...(p.deleted || [])]) owned.add(name);
      if(event.type==='history_truncate') actions=actions.filter(({event:old})=>!['summary','history_truncate'].includes(old.type)
        && (old.type!=='history' || (old.payload.step || 0)<=p.step));
      else actions.push(item);
    }
    const reducer=new SummaryReducer(),summary=Object.create(null);
    for(const {event,metrics} of actions) {
      const p=event.payload;
      if(event.type==='metric_definition') reducer.define(p);
      else if(event.type==='history') reducer.apply(Object.fromEntries(Object.entries(metrics).filter(([name])=>!name.startsWith('_tensorboard'))),summary);
      else if(event.type==='summary' && !p.automatic) {
        Object.assign(summary,clone(p.values || {}));
        for(const name of p.deleted || []) delete summary[name];
        reducer.update(p.values || {},p.deleted || []);
      }
    }
    results.set(key,{roots:owned,summary});
  }
  return results;
}

export function applyGroupSummary(summary,correction) {
  if(!correction) return;
  for(const key of Object.keys(summary)) if(key.startsWith('_tensorboard')) delete summary[key];
  for(const key of correction.roots) {
    if(Object.hasOwn(correction.summary,key)) summary[key]=clone(correction.summary[key]);
    else delete summary[key];
  }
}
