/**
 * Architecture: stored typed Table cell -> Python-compatible equality token ->
 * browser join index. Tags distinguish strings, dates, naive/aware datetimes,
 * tuples and frozensets. Boolean and numeric keys deliberately share identity.
 * Main callers: table-explorer.joinTables. No logged value is executed or used
 * as an object property. Mutable containers fail explicitly, as they do in Python.
 */
let nanIdentity=0;

function timestampToken(value) {
  if(typeof value!=='string')throw new Error('Invalid timestamp join key.');
  const match=/^(\d{4}-\d{2}-\d{2})(?:T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,6}))?([+-]\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?|Z)?)?$/.exec(value);
  if(!match)throw new Error('Invalid timestamp join key.');
  const [,day,hour,minute,second,fraction,zone]=match;
  const midnight=Date.parse(day+'T00:00:00Z');
  if(!Number.isFinite(midnight)||new Date(midnight).toISOString().slice(0,10)!==day||Number(day.slice(0,4))<1)
    throw new Error('Invalid timestamp join key.');
  if(hour===undefined)return ['date',day];
  if(+hour>23||+minute>59||+second>59)throw new Error('Invalid timestamp join key.');
  const micros=text=>BigInt((text || '').padEnd(6,'0'));
  let instant=BigInt(midnight)*1000n+BigInt(+hour*3600+ +minute*60+ +second)*1000000n+micros(fraction);
  if(zone&&zone!=='Z') {
    const offset=/^([+-])(\d{2}):(\d{2})(?::(\d{2})(?:\.(\d{1,6}))?)?$/.exec(zone);
    const [,sign,h,m,s,f]=offset;
    if(+h>23||+m>59||+(s||0)>59)throw new Error('Invalid timestamp join key.');
    const delta=BigInt(+h*3600+ +m*60+ +(s||0))*1000000n+micros(f);
    instant-=sign==='+'?delta:-delta;
  }
  return [zone?'aware-datetime':'naive-datetime',instant.toString()];
}

function token(value) {
  if(value===null)return ['none'];
  if(typeof value==='string')return ['str',value];
  if(typeof value==='boolean')return ['number',Number(value)];
  if(typeof value==='number') {
    if(Number.isNaN(value))return ['nan',++nanIdentity];
    return ['number',Number.isFinite(value)?value:(value>0?'Infinity':'-Infinity')];
  }
  if(value && typeof value==='object' && !Array.isArray(value)) {
    if(value._type==='number') {
      if(!['NaN','Infinity','-Infinity'].includes(value.value))throw new Error('Invalid number join key.');
      return token(Number(value.value));
    }
    if(value._type==='timestamp')return timestampToken(value.value);
    if(value._type==='python.sequence' && Array.isArray(value.data)) {
      if(value.kind==='tuple')return ['tuple',value.data.map(token)];
      if(value.kind==='frozenset')return ['frozenset',[...new Set(value.data.map(item=>JSON.stringify(token(item))))].sort()];
    }
  }
  throw new Error('JoinedTable keys must be scalar or immutable hashable values');
}

export function tableJoinKey(value) { return JSON.stringify(token(value)); }
