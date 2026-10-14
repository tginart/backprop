/**
 * Architecture: graph file text -> JSON grammar with Python's three non-finite
 * constants -> ordinary values. No expressions or executable text are accepted.
 * Main callers: graph hydration and replay checks. Python's pinned Graph encoder
 * writes NaN/Infinity, so response.json() would reject otherwise valid captures.
 */
export function parseGraphJSON(text) {
  if (typeof text !== 'string') throw new TypeError('Graph JSON must be text.');
  let offset=0;
  const stringToken=/"(?:[^"\\\u0000-\u001f]|\\(?:["\\/bfnrt]|u[0-9a-fA-F]{4}))*"/y;
  const numberToken=/-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?/y;
  function fail() {throw new SyntaxError(`Invalid graph JSON at position ${offset}.`);}
  function space() {while(/[ \t\r\n]/.test(text[offset] || '\x00')) offset++;}
  function string() {
    stringToken.lastIndex=offset; const token=stringToken.exec(text); if(!token)fail();
    offset=stringToken.lastIndex; return JSON.parse(token[0]);
  }
  function value() {
    space(); const first=text[offset];
    if(first==='"') return string();
    if(first==='[' || first==='{') {
      const array=first==='[', result=array?[]:{}, end=array?']':'}'; offset++;space();
      if(text[offset]===end) {offset++;return result;}
      while(true) {
        if(array) result.push(value());
        else {
          space();const key=string();space();if(text[offset++]!==':')fail();
          Object.defineProperty(result,key,{value:value(),writable:true,enumerable:true,configurable:true});
        }
        space();const next=text[offset++];if(next===end)return result;if(next!==',')fail();
      }
    }
    for(const [token,number] of [['NaN',NaN],['Infinity',Infinity],['-Infinity',-Infinity],['true',true],['false',false],['null',null]]) {
      if(text.startsWith(token,offset)) {offset+=token.length;return number;}
    }
    numberToken.lastIndex=offset; const token=numberToken.exec(text);if(!token)fail();
    offset=numberToken.lastIndex;return Number(token[0]);
  }
  const result=value();space();if(offset!==text.length)fail();return result;
}

/** Keep non-finite measurements visible in the text inspector.
 * Main caller: graphViewer. The decoded graph retains numbers; only the human
 * readable display uses their names so JSON.stringify cannot turn them to null.
 */
export function inspectGraphValue(value) {
  return JSON.stringify(value,(_key,item)=>typeof item==='number'&&!Number.isFinite(item)?String(item):item,2);
}
