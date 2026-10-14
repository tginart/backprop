/** Tooltip regression: real clipped panels -> top-layer hover -> viewport bounds.
 * Main caller: node tests/browser_chart_tooltip_qa.mjs. Uses an isolated browser,
 * no Foundry authentication. Checks clipping/cleanup first, then pointer, zoom
 * and keyboard coordinates in wide, tall and scaled chart panels.
 */
import assert from 'node:assert/strict';
import {createServer} from 'node:http';
import {readFile} from 'node:fs/promises';
const {chromium}=await import(process.env.PLAYWRIGHT_MODULE || 'playwright');
const html=`<link rel="stylesheet" href="/styles.css"><style>body{height:2000px}#panel{position:absolute;right:0;top:250px;width:300px;height:125px;overflow:hidden;transform:translateZ(0)}</style><div id="panel"></div><script type="module">import {chartSpec,renderChart,bindChart} from '/charts.mjs';const spec=chartSpec(Array.from({length:12},(_,i)=>({name:'Experiment '+i,color:'#abcdef',points:[{x:0,y:i},{x:1,y:i+1}]})));const panel=document.querySelector('#panel');panel.innerHTML=renderChart(spec);bindChart(panel,spec,()=>{});window.ready=true;</script>`;
const server=createServer(async(req,res)=>{try{const path=new URL(req.url,'http://localhost').pathname;res.setHeader('Content-Type',path==='/'?'text/html':path.endsWith('.css')?'text/css':'text/javascript');res.end(path==='/'?html:await readFile(new URL('../web'+path,import.meta.url)));}catch{res.writeHead(404);res.end();}});
await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
const browser=await chromium.launch({headless:true,executablePath:process.env.BROWSER_EXECUTABLE});
try {
 const page=await browser.newPage({viewport:{width:600,height:400}}),errors=[];
 page.on('pageerror',error=>errors.push(error.message));
 await page.goto(`http://127.0.0.1:${server.address().port}`);await page.waitForFunction(()=>window.ready);
 const svg=page.locator('svg'),tip=page.locator('.chart-tooltip');
 await svg.dispatchEvent('pointermove',{clientX:590,clientY:365});
 assert(await tip.evaluate(el=>el.matches(':popover-open')));
 let box=await tip.boundingBox();assert(box.x>=8 && box.y>=8 && box.x+box.width<=592 && box.y+box.height<=392);
 assert(box.y<250,'tooltip escapes the clipped panel');
 assert(await tip.evaluate(el=>document.elementFromPoint(el.getBoundingClientRect().x+5,el.getBoundingClientRect().y+5)===el));
 assert(await tip.evaluate(el=>el.scrollHeight>el.clientHeight),'tall content scrolls');
 await tip.evaluate(el=>{el.scrollTop=el.scrollHeight});assert(await tip.evaluate(el=>el.scrollTop>0));
 await svg.focus();await page.keyboard.press('End');assert(await tip.isVisible());await page.keyboard.press('Escape');assert(await tip.isHidden());
 await page.keyboard.press('Home');await page.evaluate(()=>scrollTo(0,100));await page.waitForFunction(()=>document.querySelector('.chart-tooltip').hidden);
 await page.evaluate(()=>scrollTo(0,0));await page.keyboard.press('End');await page.setViewportSize({width:320,height:400});await page.waitForFunction(()=>document.querySelector('.chart-tooltip').hidden);
 await page.keyboard.press('Home');box=await tip.boundingBox();assert(box.x>=8&&box.x+box.width<=312);
 await page.screenshot({path:'/tmp/backprop-tooltip-fixed.png'});
 await page.evaluate(()=>document.querySelector('#panel').remove());await page.waitForTimeout(50);assert.equal(await page.locator(':popover-open').count(),0);
 // Reuse the same bound chart while resizing, as workspace panels do.
 await page.setViewportSize({width:1400,height:900});
 await page.evaluate(async()=>{
  const {chartSpec,renderChart,bindChart}=await import('/charts.mjs');
  const panel=document.createElement('div');panel.id='coordinate-panel';
  panel.style.cssText='position:absolute;left:40px;top:60px;transform:scale(.9);transform-origin:top left';
  document.body.append(panel);
  const spec=chartSpec([{name:'Coordinate fixture',color:'#abcdef',points:Array.from({length:101},(_,x)=>({x,y:x}))}],{xMin:0,xMax:100,yMin:0,yMax:100});
  panel.innerHTML=renderChart(spec);panel.querySelector('svg').style.cssText='width:100%;height:100%';
  window.coordinateZooms=[];bindChart(panel,spec,range=>window.coordinateZooms.push(range));
 });
 const coordinateSvg=page.locator('#coordinate-panel svg');
 for(const [width,height] of [[900,250],[400,250],[600,250],[900,375]]){
  await page.locator('#coordinate-panel').evaluate((el,[w,h])=>{el.style.width=w+'px';el.style.height=h+'px'},[width,height]);
  const bounds=await coordinateSvg.boundingBox();
  // Independent geometry oracle: default SVG centers the aspect-preserved viewBox.
  const scale=Math.min(bounds.width/600,bounds.height/250);
  const left=bounds.x+(bounds.width-600*scale)/2,top=bounds.y+(bounds.height-250*scale)/2;
  const point=(x,y)=>({x:left+(76+x/100*506)*scale,y:top+(218-y/100*200)*scale});
  const hover=point(25,50);
  await page.mouse.move(hover.x,hover.y);
  assert.equal(await page.locator('#coordinate-panel .tooltip-title').textContent(),'Step 25');
  const crosshair=await coordinateSvg.locator('.crosshair').boundingBox();
  assert(Math.abs(crosshair.x+crosshair.width/2-hover.x)<.1,`crosshair follows cursor at ${width}x${height}: ${JSON.stringify(crosshair)} versus ${JSON.stringify(hover)}`);
  const start=point(20,20),end=point(80,80);
  await page.mouse.move(start.x,start.y);await page.mouse.down();await page.mouse.move(end.x,end.y);await page.mouse.up();
  const range=await page.evaluate(()=>window.coordinateZooms.at(-1));
  assert.equal(range.length,4);
  range.forEach((value,i)=>assert(Math.abs(value-[20,80,20,80][i])<.1,`zoom axis ${i} at ${width}x${height}: ${value}`));
  await coordinateSvg.focus();await page.keyboard.press('Home');
  const anchor=point(0,100),keyboardTip=await page.locator('#coordinate-panel .chart-tooltip').boundingBox();
  assert(Math.abs(keyboardTip.x-anchor.x-12)<.1,'keyboard tooltip uses drawn plot X');
  assert(Math.abs(keyboardTip.y-anchor.y-12)<.1,'keyboard tooltip uses drawn plot Y');
  await page.keyboard.press('Escape');
 }
 assert.deepEqual(errors,[]);console.log('PASS: clipping, viewport edges, scrolling, keyboard, resize, removal, pointer alignment and XY zoom across aspect ratios; no browser errors');
} finally {await browser.close();await new Promise(resolve=>server.close(resolve));}
