/** Read NumPy's numeric NPZ columns without executing the Python header.
 * Architecture: exact ZIP member -> bounded raw-deflate -> NPY metadata ->
 * typed values reshaped in C or Fortran order. Main caller: public Table reader.
 * Pickles, structured and extended-precision dtypes fail explicitly. Nothing
 * in an array may select a network destination or execute code in the browser.
 */
const limit=256*1024*1024;
const text=new TextDecoder('utf-8',{fatal:true});
const fail=message=>{throw Error(`Cannot read NumPy table column: ${message}`);};
function bytes(value){return value instanceof Uint8Array?value:new Uint8Array(value);}
function crc32(data){let crc=0xffffffff;for(const byte of data){crc^=byte;for(let bit=0;bit<8;bit++)crc=(crc>>>1)^((crc&1)?0xedb88320:0);}return (crc^0xffffffff)>>>0;}

/** Select a single stored/deflated member through the ZIP central directory.
 * Main caller: readNpz. Central sizes handle NumPy's ZIP64 local placeholders.
 * Reject duplicate names, encryption, unexpected sizes and damaged CRC values.
 */
export async function readNpz(value,key){
 const data=bytes(value);if(data.length>limit)fail('file exceeds the browser size limit');
 const view=new DataView(data.buffer,data.byteOffset,data.byteLength);let end=-1;
 for(let i=data.length-22;i>=Math.max(0,data.length-65557);i--)if(view.getUint32(i,true)===0x06054b50 && i+22+view.getUint16(i+20,true)===data.length){end=i;break;}
 if(end<0)fail('ZIP directory is absent');
 if(view.getUint16(end+4,true)||view.getUint16(end+6,true))fail('multi-disk archive');
 const count=view.getUint16(end+10,true),directorySize=view.getUint32(end+12,true),directory=view.getUint32(end+16,true);
 if(directory+directorySize!==end || directory>end)fail('invalid ZIP directory bounds');
 let offset=directory,entry;const names=new Set();
 for(let index=0;index<count;index++){
  if(offset+46>end||view.getUint32(offset,true)!==0x02014b50)fail('invalid ZIP entry');
  const nameLength=view.getUint16(offset+28,true),extra=view.getUint16(offset+30,true),comment=view.getUint16(offset+32,true),next=offset+46+nameLength+extra+comment;
  if(next>end)fail('truncated ZIP entry');
  const name=text.decode(data.subarray(offset+46,offset+46+nameLength));if(names.has(name))fail('duplicate ZIP member');names.add(name);
  if(name===key+'.npy')entry={flags:view.getUint16(offset+8,true),method:view.getUint16(offset+10,true),crc:view.getUint32(offset+16,true),compressed:view.getUint32(offset+20,true),size:view.getUint32(offset+24,true),offset:view.getUint32(offset+42,true)};
  offset=next;
 }
 if(offset!==end||!entry)fail('requested array member is absent');
 if(entry.flags&1 || ![0,8].includes(entry.method)||entry.size>limit)fail('unsupported ZIP encoding or size');
 const start=entry.offset;if(start+30>directory||view.getUint32(start,true)!==0x04034b50)fail('invalid local ZIP member');
 const nameLength=view.getUint16(start+26,true),body=start+30+nameLength+view.getUint16(start+28,true);
 if(body>directory||view.getUint16(start+6,true)!==entry.flags||view.getUint16(start+8,true)!==entry.method||text.decode(data.subarray(start+30,start+30+nameLength))!==key+'.npy')fail('local ZIP metadata differs from its directory');
 if(body+entry.compressed>directory)fail('truncated array bytes');
 let payload=data.subarray(body,body+entry.compressed);
 if(entry.method===8){
  const stream=new Blob([payload]).stream().pipeThrough(new DecompressionStream('deflate-raw')),reader=stream.getReader(),chunks=[];let length=0;
  try{while(true){const part=await reader.read();if(part.done)break;length+=part.value.length;if(length>entry.size||length>limit)fail('decompressed size exceeds the declared size');chunks.push(part.value);}}
  catch(error){await reader.cancel();throw error;}
  payload=new Uint8Array(length);let at=0;for(const chunk of chunks){payload.set(chunk,at);at+=chunk.length;}
 }
 if(payload.length!==entry.size||crc32(payload)!==entry.crc)fail('array checksum or size mismatch');
 return readNpy(payload);
}

function half(value){const sign=value&0x8000?-1:1,exponent=(value>>10)&31,fraction=value&1023;return exponent===31?fraction?NaN:sign*Infinity:sign*2**(exponent?exponent-15:-14)*(exponent?1+fraction/1024:fraction/1024);}
function number(value){return Number.isFinite(value)?value:{_type:'number',value:Number.isNaN(value)?'NaN':value>0?'Infinity':'-Infinity'};}
function integer(value){return value>=BigInt(Number.MIN_SAFE_INTEGER)&&value<=BigInt(Number.MAX_SAFE_INTEGER)?Number(value):{_type:'integer',value:String(value)};}

/** Parse only NPY's literal scalar dtype header, never eval or arbitrary code.
 * Main caller: readNpz; exported for corruption and dtype tests. Values beyond
 * JavaScript's integer precision retain their exact decimal representation.
 */
export function readNpy(value){
 const data=bytes(value),view=new DataView(data.buffer,data.byteOffset,data.byteLength);
 if(data.length<10||data[0]!==147||text.decode(data.subarray(1,6))!=='NUMPY')fail('invalid NPY signature');
 const version=data[6];if(![1,2,3].includes(version)||data[7]!==0)fail('unsupported NPY version');
 const begin=version===1?10:12;if(data.length<begin)fail('truncated NPY header');
 const length=version===1?view.getUint16(8,true):view.getUint32(8,true),body=begin+length;if(body>data.length)fail('truncated NPY metadata');
 const header=text.decode(data.subarray(begin,body)).trim();
 const match=header.match(/^\{\s*['"]descr['"]:\s*['"]([<>=|])([?biufcSUOmMV])(\d+)(\[[a-zA-Z0-9]+\])?['"],\s*['"]fortran_order['"]:\s*(True|False),\s*['"]shape['"]:\s*\(\s*((?:\d+\s*,?\s*)*)\),?\s*\}$/);
 if(!match)fail('structured or invalid array header');
 const [,endian,kind,width,unit='',fortran,dimensions]=match,size=Number(width),shape=dimensions.split(',').map(x=>x.trim()).filter(Boolean).map(Number);
 const count=shape.reduce((a,b)=>a*b,1),itemSize=kind==='U'?size*4:size;
 let nodes=1,prefix=1;for(const axis of shape){prefix*=axis;nodes+=prefix;}
 if(shape.length>32||!Number.isSafeInteger(nodes)||nodes>4_000_000)fail('array exceeds the browser element limit');
 if(!Number.isSafeInteger(count)||shape.some(x=>!Number.isSafeInteger(x))||!Number.isSafeInteger(itemSize)||count*itemSize!==data.length-body)fail('array shape does not match its bytes');
 if(kind==='O'||kind==='V')fail('object or opaque array dtype');
 const little=endian!=='>';
 const float=(at,width)=>width===2?half(view.getUint16(at,little)):width===4?view.getFloat32(at,little):width===8?view.getFloat64(at,little):fail('extended-precision float dtype');
 function cell(index){
  const at=body+index*itemSize;
  if(kind==='b'||kind==='?'){if(size!==1)fail('boolean width');return !!view.getUint8(at);}
  if(kind==='i'||kind==='u'){
   const signed=kind==='i';if(size===8)return integer(signed?view.getBigInt64(at,little):view.getBigUint64(at,little));
   const method={1:signed?'getInt8':'getUint8',2:signed?'getInt16':'getUint16',4:signed?'getInt32':'getUint32'}[size];if(!method)fail('integer width');return view[method](at,little);
  }
  if(kind==='f')return number(float(at,size));
  if(kind==='c')return {_type:'complex',real:number(float(at,size/2)),imaginary:number(float(at+size/2,size/2))};
  if(kind==='S')return {_type:'bytes',hex:[...data.subarray(at,at+size)].map(x=>x.toString(16).padStart(2,'0')).join('').replace(/(?:00)+$/,'')};
  if(kind==='U'){const points=Array.from({length:size},(_,i)=>view.getUint32(at+i*4,little));while(points.at(-1)===0)points.pop();return points.map(point=>String.fromCodePoint(point)).join('');}
  if((kind==='M'||kind==='m')&&size===8)return {_type:kind==='M'?'numpy.datetime64':'numpy.timedelta64',value:String(view.getBigInt64(at,little)),unit:unit.slice(1,-1)};
  fail('array dtype');
 }
 function nest(depth,indices){if(depth===shape.length){let index=0,stride=1;const axes=shape.map((_,i)=>i);if(fortran!=='True')axes.reverse();for(const axis of axes){index+=indices[axis]*stride;stride*=shape[axis];}return cell(index);}return Array.from({length:shape[depth]},(_,i)=>nest(depth+1,[...indices,i]));}
 return {shape,dtype:endian+kind+width+unit,data:nest(0,[])};
}
