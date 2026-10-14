/**
 * Architecture: inherited defaults -> pure editor draft checks -> optional real
 * browser Apply/Cancel/reset journey. Main caller: node --test this file.
 * Set PANEL_EDITOR_BROWSER=1 to use the existing Playwright and Brave binaries.
 * The isolated browser serves only local modules and no experiment credentials.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import {resolveEditorDraft, validateEditorDraft, editorOverrides} from '../web/panel-editor.mjs';

const defaults = {xAxis:'_step', smoothing:.2, smoothingMethod:'twema', logScale:false};

test('editor run limit and axis labels inherit concrete defaults without saved overrides', () => {
  const draft = resolveEditorDraft(defaults, {}, 'loss');
  assert.equal(draft.maxRuns, 10); assert.equal(draft.xLabel, ''); assert.equal(draft.yLabel, '');
  assert.equal(draft.yMetric, 'loss'); assert.equal(draft.showLegend, true);
  assert.deepEqual(editorOverrides(draft, defaults, 'loss'), {});
  const inherited = resolveEditorDraft({...defaults, maxRuns:25, xLabel:'Epoch', yLabel:'Objective', showLegend:false}, {}, 'loss');
  assert.equal(inherited.maxRuns, 25); assert.equal(inherited.xLabel, 'Epoch'); assert.equal(inherited.yLabel, 'Objective'); assert.equal(inherited.showLegend, false);
});

test('draft editing and inherited reset never mutate saved settings or defaults', () => {
  const saved = {smoothing:.6, maxRuns:5, xLabel:'Iteration', yMin:0};
  const draft = resolveEditorDraft(defaults, saved, 'loss');
  draft.smoothing = .8; draft.maxRuns = 20;
  assert.deepEqual(saved, {smoothing:.6, maxRuns:5, xLabel:'Iteration', yMin:0});
  const reset = resolveEditorDraft(defaults, {}, 'loss');
  assert.equal(reset.smoothing, .2); assert.equal(reset.maxRuns, 10);
  assert.deepEqual(editorOverrides(reset, defaults, 'loss'), {});
  assert.equal(defaults.smoothing, .2);
});

test('only explicit changes are applied and empty axis bounds remove old overrides', () => {
  const draft = resolveEditorDraft(defaults, {title:'Objective', yMetric:'eval/loss', maxRuns:20, xLabel:'Batch', showLegend:false}, 'loss');
  assert.deepEqual(editorOverrides(draft, defaults, 'loss'), {title:'Objective', yMetric:'eval/loss', maxRuns:20, xLabel:'Batch', showLegend:false});
  assert(!Object.hasOwn(editorOverrides(resolveEditorDraft(defaults, {}, 'loss'), defaults, 'loss'), 'yMin'));
});

test('run limits reject fractions, out-of-range and nonnumeric values', () => {
  const draft = resolveEditorDraft(defaults, {}, 'loss');
  for (const maxRuns of [0,-1,1.5,1001,NaN,Infinity,'10',null]) assert.match(validateEditorDraft({...draft,maxRuns}), /whole number between 1 and 1000/);
  for (const maxRuns of [1,10,1000,undefined]) assert.equal(validateEditorDraft({...draft,maxRuns}), '');
  assert.match(validateEditorDraft({...draft,xLabel:3}), /Axis labels/);
  assert.match(validateEditorDraft({...draft,yLabel:null}), /Axis labels/);
  assert.match(validateEditorDraft({...draft,xMin:10,xMax:2}), /minimum/);
});

test('browser editor resets only a draft, cancels cleanly and applies validated overrides', {skip:!process.env.PANEL_EDITOR_BROWSER}, async context => {
  const {createServer} = await import('node:http'), {readFile} = await import('node:fs/promises');
  const {chromium} = await import(process.env.PLAYWRIGHT_MODULE || 'playwright');
  const html = `<!doctype html><link rel="stylesheet" href="/styles.css"><link rel="stylesheet" href="/workspace.css"><dialog id="editor"></dialog><script type="module">
    import {openPanelEditor} from '/panel-editor.mjs';
    window.applied=[];window.previews=[];
    window.openEditor=settings=>openPanelEditor({dialog:document.querySelector('#editor'),metric:'loss',metrics:['loss','eval/loss'],
      defaults:${JSON.stringify(defaults)},settings,renderPreview:(node,draft)=>{window.previews.push(structuredClone(draft));node.textContent=JSON.stringify(draft)},
      onApply:overrides=>window.applied.push(structuredClone(overrides))});
  </script>`;
  const server = createServer(async (request, response) => {
    if (request.url === '/') {response.setHeader('Content-Type','text/html'); response.end(html); return;}
    if (!/^\/[a-z0-9.-]+\.(mjs|css)$/.test(request.url)) {response.writeHead(404); response.end(); return;}
    try {response.setHeader('Content-Type',request.url.endsWith('.css') ? 'text/css' : 'text/javascript');response.end(await readFile(new URL('../web'+request.url, import.meta.url)));}
    catch {response.writeHead(404);response.end();}
  });
  await new Promise(resolve => server.listen(0,'127.0.0.1',resolve));
  context.after(() => new Promise(resolve => server.close(resolve)));
  const browser = await chromium.launch({headless:true, executablePath:process.env.CHROMIUM_EXECUTABLE});
  context.after(() => browser.close());
  const page = await browser.newPage({viewport:{width:1440,height:1000}}), errors=[];
  page.on('pageerror',error=>errors.push(error.message));
  await page.goto(`http://127.0.0.1:${server.address().port}`);
  await page.waitForFunction(()=>typeof window.openEditor==='function');
  const open = settings => page.evaluate(value=>window.openEditor(value),settings);
  await open({smoothing:.6,maxRuns:5,xLabel:'Saved label'});
  await page.getByRole('tab',{name:'Display',exact:true}).click();
  await page.getByLabel('Max runs',{exact:true}).fill('20');
  await page.getByRole('button',{name:'Use inherited defaults'}).click();
  assert(await page.locator('#editor').evaluate(dialog=>dialog.open));
  assert.equal(await page.getByLabel('Max runs',{exact:true}).inputValue(),'10');
  assert.equal(await page.getByLabel('X axis label',{exact:true}).inputValue(),'');
  assert.equal(await page.evaluate(()=>window.applied.length),0);
  await page.getByRole('button',{name:'Cancel',exact:true}).click();
  assert.equal(await page.evaluate(()=>window.applied.length),0);
  await open({smoothing:.6});
  await page.getByRole('button',{name:'Use inherited defaults'}).click();
  await page.keyboard.press('Escape');
  assert.equal(await page.locator('#editor').evaluate(dialog=>dialog.open),false);
  assert.equal(await page.evaluate(()=>window.applied.length),0);

  await open({});
  await page.getByLabel('Panel title',{exact:true}).fill('Training convergence');
  await page.getByRole('tab',{name:'Data',exact:true}).focus();
  await page.keyboard.press('ArrowRight');
  assert.equal(await page.getByRole('tab',{name:'Display',exact:true}).getAttribute('aria-selected'),'true');
  assert.equal(await page.getByRole('tabpanel',{name:'Data',exact:true}).count(),0);
  for (const invalid of ['0','1.5','1001']) {
    await page.getByLabel('Max runs',{exact:true}).fill(invalid);
    assert(await page.getByRole('button',{name:'Apply settings'}).isDisabled());
    assert.match(await page.getByRole('alert').textContent(),/whole number between 1 and 1000/);
  }
  await page.getByLabel('Max runs',{exact:true}).fill('20');
  await page.getByLabel('X axis label',{exact:true}).fill('Batch');
  await page.getByLabel('Y axis label',{exact:true}).fill('Objective');
  await page.getByLabel('Show run legend and latest values',{exact:true}).uncheck();
  assert.match(await page.locator('[data-preview-limit]').textContent(),/Preview run limit: 20/);
  await page.getByRole('tab',{name:'Data',exact:true}).click();
  await page.getByLabel('Y metric',{exact:true}).selectOption('eval/loss');
  await page.getByRole('button',{name:'Apply settings'}).click();
  assert.deepEqual(await page.evaluate(()=>window.applied),[{title:'Training convergence',yMetric:'eval/loss',maxRuns:20,xLabel:'Batch',yLabel:'Objective',showLegend:false}]);
  await open({smoothing:.6,maxRuns:2});
  await page.getByRole('button',{name:'Use inherited defaults'}).click();
  await page.getByRole('button',{name:'Apply settings'}).click();
  assert.deepEqual(await page.evaluate(()=>window.applied.at(-1)),{});
  assert.deepEqual(errors,[]);
});
