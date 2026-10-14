/** Immutable source observations -> deterministic grouped TensorBoard history.
 * Main caller: TensorBoardHistory. Mirrors tensorboard_group_replay.py and the
 * importer grouping protocol. Physical writer events remain untouched; missing
 * positions are visible incomplete data and conflicting observations throw.
 */
import {originIdentity, semanticValue} from './tensorboard-replay.mjs';
import {sha256} from './sha256.mjs';
const encode = value => new TextEncoder().encode(value);
const hexBytes = value => Uint8Array.from(value.match(/../g), item => parseInt(item,16));
const same = (a,b) => JSON.stringify(semanticValue(a)) === JSON.stringify(semanticValue(b));
const fieldsEqual = (value, fields) => value && typeof value === 'object' && !Array.isArray(value)
  && Object.keys(value).sort().join(',') === fields.slice().sort().join(',');
const memberFields = ['origin','index','step','wall_time','metrics'];
const position = member => [member.origin.filename, member.index];
const compareText = (a,b) => {
  const x=Array.from(a), y=Array.from(b);
  for(let i=0;i<Math.min(x.length,y.length);i++) if(x[i]!==y[i]) return x[i].codePointAt(0)-y[i].codePointAt(0);
  return x.length-y.length;
};
const comparePosition = (a,b) => compareText(a[0],b[0]) || a[1]-b[1];

function validateMember(member, boundary=false) {
  if (!fieldsEqual(member,boundary?memberFields.filter(key=>key!=='metrics'):memberFields)) throw new Error('TensorBoard group member has unexpected fields');
  originIdentity(member.origin);
  if (!Number.isSafeInteger(member.index) || member.index<0) throw new Error('TensorBoard event index must be an exact nonnegative JSON integer');
  if (typeof member.step!=='string' || !/^(0|-?[1-9][0-9]*)$/.test(member.step)
      || BigInt(member.step)<-(2n**63n) || BigInt(member.step)>=2n**63n) throw new Error('TensorBoard source step must be a canonical signed-int64 string');
  if (typeof member.wall_time!=='number' || !Number.isFinite(member.wall_time)) throw new Error('TensorBoard source wall time must be finite');
  if (!boundary && (!member.metrics || typeof member.metrics!=='object' || Array.isArray(member.metrics))) throw new Error('TensorBoard member metrics must be a string-keyed map');
}

export function validateGroup(group, record, origin) {
  if (!fieldsEqual(group,['version','prefix','members','next','revision']) || group.version!==1) throw new Error('TensorBoard group has unexpected fields or version');
  if (!Array.isArray(group.prefix) || !Array.isArray(group.members) || !group.members.length) throw new Error('TensorBoard groups need list prefixes and nonempty member lists');
  const {prefix,members,next}=group;
  for (const item of [...prefix,...members]) validateMember(item);
  if (prefix.some(item=>Object.keys(item.metrics).length) || !members.some(item=>Object.keys(item.metrics).length)) throw new Error('Invalid TensorBoard group metric coverage');
  if (members.some(item=>item.step!==members[0].step) || prefix.length && prefix.at(-1).step===members[0].step) throw new Error('TensorBoard group members must share one exact source step');
  const evidence=[...prefix,...members];
  if(next!==null) {
    validateMember(next,true);
    if(next.step===members[0].step) throw new Error('TensorBoard next event must prove a different-step barrier');
    evidence.push(next);
  }
  for(const item of evidence) if(item.origin.namespace!==members[0].origin.namespace) throw new Error('TensorBoard group crosses source namespaces');
  for(let i=1;i<evidence.length;i++) {
    const before=position(evidence[i-1]),after=position(evidence[i]);
    if(comparePosition(after,before)<=0 || before[0]===after[0] && after[1]!==before[1]+1 || before[0]!==after[0] && after[1]!==0)
      throw new Error('TensorBoard group evidence has a missing or reordered source position');
  }
  const chunks=[encode('tensorboard-group-v1\0'),...prefix.map(item=>hexBytes(originIdentity(item.origin))),
    encode('\0members\0'),...members.map(item=>hexBytes(originIdentity(item.origin))),encode('\0next\0'),
    ...(next===null?[]:[hexBytes(originIdentity(next.origin))])];
  const bytes=new Uint8Array(chunks.reduce((n,item)=>n+item.length,0));let offset=0;
  for(const chunk of chunks){bytes.set(chunk,offset);offset+=chunk.length;}
  if(group.revision!==sha256(bytes)) throw new Error('TensorBoard group revision does not match its source evidence');
  if(record!==originIdentity(origin) || !same(origin,members[0].origin)) throw new Error('TensorBoard group anchor does not match its first member');
}

function groupMetrics(group) {
  const first=group.members[0],last=group.members.at(-1),metrics={};
  for(const item of group.members) Object.assign(metrics,item.metrics);
  return {...metrics,global_step:Number(last.step),_timestamp:last.wall_time,
    _tensorboard_record:originIdentity(first.origin),_tensorboard_origin:first.origin,
    _tensorboard_source:first.origin.filename,_tensorboard_group:group};
}

function* metricLeaves(mapping,prefix=[]) {
  for(const [key,value] of Object.entries(mapping)) {
    const path=[...prefix,key];
    if(value && typeof value==='object' && !Array.isArray(value) && !('_type' in value)) yield* metricLeaves(value,path);
    else yield path;
  }
}

function applyStepSync(source,annotations,converged=false) {
  if(!Array.isArray(annotations)) throw new Error('TensorBoard step-sync annotations must be a list');
  const result=Object.fromEntries(Object.entries(source).map(([key,value])=>[key,structuredClone(value)])),
    paths=[...metricLeaves(source)],seen=[];
  for(const item of annotations) {
    if(!fieldsEqual(item,['path','value'])) throw new Error('Invalid TensorBoard step-sync annotation');
    const path=item.path;
    if(!Array.isArray(path) || !path.length || path.some(key=>typeof key!=='string') || path[0].startsWith('_tensorboard'))
      throw new Error('Invalid TensorBoard step-sync path');
    const overlaps=other=>path.slice(0,other.length).every((key,i)=>key===other[i]) && path.length>=other.length
      || other.slice(0,path.length).every((key,i)=>key===path[i]) && other.length>=path.length;
    if(seen.some(overlaps)) throw new Error('Overlapping TensorBoard step-sync paths');
    seen.push(path);
    if(paths.some(overlaps)) {
      if(converged) continue;
      throw new Error('TensorBoard step-sync annotation overwrites source evidence');
    }
    let target=result;
    for(const key of path.slice(0,-1)) {
      if(!Object.hasOwn(target,key)) target[key]=Object.create(null);
      target=target[key];
    }
    Object.defineProperty(target,path.at(-1),{value:structuredClone(item.value),enumerable:true,writable:true,configurable:true});
  }
  return result;
}

function survivingHistory(events) {
  const retained=new Map(),scope=event=>JSON.stringify([event.entity || '',event.project,event.run_id]);
  for(const event of events) {
    if(event.type==='history') retained.set(event.event_id,event);
    else if(event.type==='history_truncate') for(const [key,old] of retained)
      if(scope(old)===scope(event) && (old.payload.step || 0)>event.payload.step) retained.delete(key);
  }
  return events.filter(event=>event.type!=='history' || retained.has(event.event_id));
}

export function projectGroups(events) {
  const streams=new Map(),sourceIDs=new Map(),consumed=new Set(),legacy=[];
  const surviving=survivingHistory(events),retainedIDs=new Set(surviving.map(event=>event.event_id));
  for(const event of events) if(!retainedIDs.has(event.event_id) && event.payload.metrics?._tensorboard_origin) consumed.add(event.event_id);
  for(const [order,event] of surviving.entries()) {
    if(event.type!=='history') continue;
    const metrics=event.payload.metrics || {},group=metrics._tensorboard_group;
    if(group===undefined) {if(Object.hasOwn(metrics,'_tensorboard_origin')) legacy.push(event);continue;}
    validateGroup(group,metrics._tensorboard_record,metrics._tensorboard_origin);
    const actual=Object.fromEntries(Object.entries(metrics).filter(([key])=>!['_runtime','_step'].includes(key)));
    let expected=groupMetrics(group);
    if(Object.hasOwn(metrics,'_tensorboard_step_sync')) {
      expected=applyStepSync(expected,metrics._tensorboard_step_sync);
      expected._tensorboard_step_sync=metrics._tensorboard_step_sync;
    }
    if(!same(actual,expected)) throw new Error('TensorBoard group metrics disagree with member evidence');
    consumed.add(event.event_id);
    const scope=[event.entity || '',event.project,event.run_id],stream=JSON.stringify([...scope,metrics._tensorboard_origin.namespace]);
    if(!streams.has(stream)) streams.set(stream,new Map());
    const observations=[...group.prefix,...group.members,...(group.next===null?[]:[group.next])];
    for(const member of observations) {
      const pos=JSON.stringify(position(member)),source=JSON.stringify([...scope,originIdentity(member.origin)]);
      if(sourceIDs.has(source) && !same(sourceIDs.get(source),[stream,pos])) throw new Error('TensorBoard source identity has conflicting physical positions');
      sourceIDs.set(source,[stream,pos]);
      const previous=streams.get(stream).get(pos);
      if(previous) {
        const before=previous.member;
        if(['origin','index','step','wall_time'].some(key=>!same(before[key],member[key]))) throw new Error('Conflicting TensorBoard source position');
        if(Object.hasOwn(before,'metrics') && Object.hasOwn(member,'metrics')) {
          if(!same(before.metrics,member.metrics)) throw new Error('Conflicting TensorBoard member metrics');
          continue;
        }
        if(Object.hasOwn(before,'metrics')) continue;
      }
      streams.get(stream).set(pos,{member:structuredClone(member),event,order});
    }
  }
  const markers=new Set(['_tensorboard_record','_tensorboard_origin','_tensorboard_source','_runtime','_step']);
  for(const event of legacy) {
    const metrics=event.payload.metrics,identity=originIdentity(metrics._tensorboard_origin);
    if(metrics._tensorboard_record!==identity) throw new Error('TensorBoard record identity does not match its origin');
    const source=JSON.stringify([event.entity || '',event.project,event.run_id,identity]);
    if(!sourceIDs.has(source)) continue;
    const [stream,pos]=sourceIDs.get(source),member=streams.get(stream).get(pos).member;
    if(!Object.hasOwn(member,'metrics')) continue;
    const expected={...member.metrics,global_step:Number(member.step),_timestamp:member.wall_time};
    const actual=Object.fromEntries(Object.entries(metrics).filter(([key])=>!markers.has(key)));
    if(!same(actual,expected)) throw new Error('Legacy TensorBoard row conflicts with grouped source evidence');
    consumed.add(event.event_id);
  }
  const replacements=new Map();
  for(const positions of streams.values()) {
    let pending=[],incomplete=false,previous=null;
    const emit=()=>{
      if(!pending.length || !pending.some(item=>Object.keys(item.member.metrics || {}).length)) return;
      const first=pending[0].member,last=pending.at(-1).member;
      const owner=pending.reduce((a,b)=>a.order<b.order?a:b).event;let metrics={};
      for(const item of pending) Object.assign(metrics,item.member.metrics);
      Object.assign(metrics,{global_step:Number(last.step),_timestamp:last.wall_time,
        _tensorboard_record:originIdentity(first.origin),_tensorboard_origin:structuredClone(first.origin),_tensorboard_source:first.origin.filename,
        _tensorboard_group_position:[first.origin.namespace,first.origin.filename,first.index],
        _tensorboard_group_members:pending.map(item=>originIdentity(item.member.origin))});
      metrics=applyStepSync(metrics,owner.payload.metrics._tensorboard_step_sync || [],true);
      if(incomplete) metrics._tensorboard_incomplete=true;
      if(!replacements.has(owner.event_id)) replacements.set(owner.event_id,[]);
      replacements.get(owner.event_id).push(metrics);
    };
    for(const item of [...positions.values()].sort((a,b)=>comparePosition(position(a.member),position(b.member)))) {
      const member=item.member,pos=position(member);
      const gap=previous===null || pos[0]!==previous[0] ? pos[1]!==0 : pos[1]!==previous[1]+1;
      if(pending.length && (gap || !Object.hasOwn(member,'metrics') || member.step!==pending.at(-1).member.step)) {
        if(gap) incomplete=true;
        emit();pending=[];incomplete=false;
      }
      if(!Object.hasOwn(member,'metrics')){previous=pos;incomplete=true;continue;}
      incomplete=incomplete || gap;pending.push(item);previous=pos;
    }
    emit();
  }
  return {replacements,consumed};
}
