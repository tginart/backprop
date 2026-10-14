/**
 * Architecture: chart specs -> escaped SVG/hover markup -> optional isolated
 * browser pointer, keyboard and zoom journeys. Main caller: node --test this file.
 * Set CHARTS_BROWSER=1 to use existing Playwright/Brave; nothing is installed.
 * The browser uses local modules only and never accesses signed-in experiments.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import {chartSpec, renderChart, renderChartTooltip} from '../web/charts.mjs';

const series = [{name:'Loss', color:'#abcdef', points:[{x:0,y:0},{x:100,y:100}]}];
const samples = count => Array.from({length:count}, (_,index) => ({name:`Experiment ${index}`, color:'#abcdef', point:{x:25,y:index}}));

test('unlabelled axes keep the original plot dimensions and numeric ticks', () => {
  const html = renderChart(chartSpec(series));
  assert.match(html, /<rect x="76" y="18" width="506" height="200"\/>/);
  assert(!html.includes('x-axis-label')); assert(!html.includes('y-axis-label'));
  assert.match(html, /y="241" text-anchor="middle"/);
});

test('custom axis titles reserve space and preserve full escaped text', () => {
  const xLabel = 'Batch <index>', yLabel = 'Objective & uncertainty', html = renderChart(chartSpec(series,{xLabel,yLabel}));
  assert.match(html, /class="chart-axis-label x-axis-label"/);
  assert.match(html, /class="chart-axis-label y-axis-label"/);
  assert.match(html, /<title>Batch &lt;index&gt;<\/title>/);
  assert.match(html, /<title>Objective &amp; uncertainty<\/title>/);
  assert(!html.includes('<index>'));
  const long = 'A long descriptive label '.repeat(6);
  assert(renderChart(chartSpec(series,{yLabel:long})).includes(`<title>${long}</title>`));
});

test('dense inspection names the omitted count and exposes every additional series', () => {
  const html = renderChartTooltip({},25,samples(20));
  assert.match(html, /12 of 20 series shown/); assert.match(html, /Show 8 more series/);
  assert.match(html, /tooltip-additional-series/); assert.match(html, /Experiment 19/);
  const expanded = renderChartTooltip({},25,samples(20),{expanded:true});
  assert.match(expanded, /20 of 20 series shown/); assert.match(expanded, /Hide 8 more series/);
  assert.match(expanded, /class="tooltip-additional-series" open/);
});

test('compact inspection keeps its existing heading and escapes run names', () => {
  const html = renderChartTooltip({xLabel:'Epoch'},25,[{name:'<img src=x>',color:'#abcdef',point:{x:25,y:1,raw:2}}]);
  assert.match(html, /class="tooltip-title">Epoch 25/);
  assert.match(html, /&lt;img src=x&gt;/); assert.match(html, /raw 2/);
  assert(!html.includes('<details')); assert(!html.includes('series shown'));
});

test('browser inspection, labels, expansion and zoom follow drawn coordinates', {skip:!process.env.CHARTS_BROWSER}, async context => {
  const {createServer} = await import('node:http'), {readFile} = await import('node:fs/promises');
  const {chromium} = await import(process.env.PLAYWRIGHT_MODULE || 'playwright');
  const html = `<!doctype html><link rel="stylesheet" href="/styles.css"><style>body{height:2000px}#panel{position:absolute;left:40px;top:60px;overflow:hidden;transform:scale(.9);transform-origin:top left}</style><div id="panel"></div><script type="module">
    import {chartSpec,renderChart,bindChart} from '/charts.mjs';
    window.installChart=options=>{
      const series=Array.from({length:20},(_,i)=>({name:'Experiment '+i,color:'#abcdef',points:[{x:0,y:i},{x:50,y:50+i},{x:100,y:100+i}]}));
      if(options.emptyFirst)series.unshift({name:'No data',color:'#abcdef',points:[]});
      window.spec=chartSpec(series,{xMin:0,xMax:100,yMin:0,yMax:150,...options});
      const panel=document.querySelector('#panel');panel.innerHTML=renderChart(window.spec);panel.querySelector('svg').style.cssText='width:100%;height:100%';
      window.zooms=[];bindChart(panel,window.spec,range=>window.zooms.push(range));
    };
  </script>`;
  const server = createServer(async (request,response) => {
    if(request.url==='/'){response.setHeader('Content-Type','text/html');response.end(html);return;}
    if(!/^\/[a-z0-9.-]+\.(mjs|css)$/.test(request.url)){response.writeHead(404);response.end();return;}
    try{response.setHeader('Content-Type',request.url.endsWith('.css')?'text/css':'text/javascript');response.end(await readFile(new URL('../web'+request.url,import.meta.url)));}
    catch{response.writeHead(404);response.end();}
  });
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  context.after(()=>new Promise(resolve=>server.close(resolve)));
  const browser = await chromium.launch({headless:true,executablePath:process.env.CHROMIUM_EXECUTABLE});
  context.after(()=>browser.close());
  const page = await browser.newPage({viewport:{width:1400,height:900}}), errors=[];
  page.on('pageerror',error=>errors.push(error.message));
  await page.goto(`http://127.0.0.1:${server.address().port}`);await page.waitForFunction(()=>typeof window.installChart==='function');
  const svg=page.locator('#panel svg'),tooltip=page.locator('#panel .chart-tooltip');
  const geometry=async()=>{
    const box=await svg.boundingBox(),rect=await svg.locator('clipPath rect').evaluate(node=>({x:Number(node.getAttribute('x')),y:Number(node.getAttribute('y')),width:Number(node.getAttribute('width')),height:Number(node.getAttribute('height'))}));
    const scale=Math.min(box.width/600,box.height/250),left=box.x+(box.width-600*scale)/2,top=box.y+(box.height-250*scale)/2;
    return (x,y)=>({x:left+(rect.x+x/100*rect.width)*scale,y:top+(rect.y+(1-y/150)*rect.height)*scale});
  };
  for(const labels of [{},{xLabel:'Batch',yLabel:'Objective'}]){
    await page.evaluate(options=>window.installChart({...options,emptyFirst:true}),labels);
    for(const [width,height] of [[900,250],[400,250],[900,375]]){
      await page.locator('#panel').evaluate((node,[w,h])=>{node.style.width=w+'px';node.style.height=h+'px'},[width,height]);
      const point=await geometry(),hover=point(25,75);
      await page.mouse.move(hover.x,hover.y);
      assert.equal(await tooltip.locator('.tooltip-title').textContent(),`${labels.xLabel || 'Step'} 25`);
      const crosshair=await svg.locator('.crosshair').boundingBox();
      assert(Math.abs(crosshair.x+crosshair.width/2-hover.x)<.1);
      const start=point(20,30),end=point(80,120);
      await page.mouse.move(start.x,start.y);await page.mouse.down();await page.mouse.move(end.x,end.y);await page.mouse.up();
      const zoom=await page.evaluate(()=>window.zooms.at(-1));
      zoom.forEach((value,index)=>assert(Math.abs(value-[20,80,30,120][index])<.1));
      await svg.focus();await page.keyboard.press('Home');
      assert.equal(await tooltip.locator('.tooltip-title').textContent(),`${labels.xLabel || 'Step'} 0`);
      const anchor=point(0,150),tipBox=await tooltip.boundingBox();
      assert(Math.abs(tipBox.x-anchor.x-12)<.1);assert(Math.abs(tipBox.y-anchor.y-12)<.1);
      await page.keyboard.press('End');assert.equal(await tooltip.locator('.tooltip-title').textContent(),`${labels.xLabel || 'Step'} 100`);
      await page.keyboard.press('Escape');assert(await tooltip.isHidden());
    }
  }

  // Dense hover must expose the count and allow keyboard access through the
  // tooltip to the remaining values, including when the viewport forces scroll.
  await page.setViewportSize({width:600,height:400});
  await page.locator('#panel').evaluate(node=>{node.style.cssText='position:absolute;left:260px;top:250px;width:300px;height:125px;overflow:hidden'});
  await page.evaluate(()=>window.installChart({}));
  let point=await geometry(),hover=point(100,0);
  await page.mouse.move(hover.x,hover.y);
  assert.equal(await tooltip.locator('[data-tooltip-series-count]').textContent(),'12 of 20 series shown');
  assert(await tooltip.evaluate(node=>node.scrollHeight>node.clientHeight));
  await svg.focus();await page.keyboard.press('Home');
  await page.keyboard.press('Tab');assert.equal(await page.evaluate(()=>document.activeElement.className),'chart-tooltip');
  await page.keyboard.press('Tab');assert.equal(await page.evaluate(()=>document.activeElement.tagName),'SUMMARY');
  await page.keyboard.press('Enter');await page.waitForFunction(()=>document.querySelector('.tooltip-additional-series').open && document.querySelector('[data-tooltip-series-count]').textContent==='20 of 20 series shown');
  assert.equal(await tooltip.locator('[data-tooltip-series-count]').textContent(),'20 of 20 series shown');
  await tooltip.evaluate(node=>{node.scrollTop=node.scrollHeight});
  const last=await tooltip.locator('.tooltip-additional-series .tooltip-series').last().boundingBox(),tipBox=await tooltip.boundingBox();
  assert(last.y>=tipBox.y && last.y+last.height<=tipBox.y+tipBox.height);
  assert(tipBox.x>=8 && tipBox.y>=8 && tipBox.x+tipBox.width<=592 && tipBox.y+tipBox.height<=392);
  await page.keyboard.press('Escape');assert(await tooltip.isHidden());

  // Reaching the floating tooltip across a small pointer gap keeps it open.
  await svg.dispatchEvent('pointermove',{clientX:hover.x,clientY:hover.y});
  await svg.dispatchEvent('pointerleave',{relatedTarget:null});
  await tooltip.dispatchEvent('pointerenter');
  await page.waitForTimeout(180);assert(await tooltip.isVisible());
  await page.keyboard.press('Escape');

  await page.setViewportSize({width:1400,height:900});
  await page.locator('#panel').evaluate(node=>{node.style.cssText='position:absolute;left:40px;top:60px;width:600px;height:250px'});
  await page.evaluate(()=>window.installChart({xLabel:'Batch',yLabel:'Objective'}));
  point=await geometry();
  const start=point(50,30),end=point(50,120);
  await page.mouse.move(start.x,start.y);await page.mouse.down();await page.mouse.move(end.x,end.y);await page.mouse.up();
  assert.deepEqual((await page.evaluate(()=>window.zooms.at(-1))).map(value=>Math.round(value)),[0,100,30,120]);
  await svg.focus();await page.keyboard.press('0');assert.equal(await page.evaluate(()=>window.zooms.at(-1)),null);
  assert.equal(await svg.locator('.x-axis-label').evaluate(node=>node.lastChild.textContent),'Batch');
  assert.equal(await svg.locator('.y-axis-label').evaluate(node=>node.lastChild.textContent),'Objective');
  assert.deepEqual(errors,[]);
});


test('wall-time inspection formats timestamps as dates in heading and samples', () => {
  const stamp=1791302400, formatted=new Date(stamp*1000).toLocaleString();
  const html=renderChartTooltip({timeAxis:true,xLabel:'Wall time'},stamp,[{name:'run',color:'#abcdef',point:{x:stamp,y:1}}]);
  assert(html.includes(`Wall time ${formatted}`));assert(html.includes(`X ${formatted}`));
});
