/**
 * Architecture: embedded chart spec + logged Table -> local Vega-Lite compiler
 * -> CSP-compatible expression interpreter -> SVG view. All chart data remains
 * local; the loader rejects external resources and clickable destinations.
 * Main callers: app.mjs rich value hydration and browser conformance tests.
 */
let libraries;
const views = new Map();
function loadScript(path) {
  return new Promise((resolve, reject) => {
    const script = document.createElement('script');
    script.src = new URL(path, import.meta.url).href;
    script.onload = resolve;
    script.onerror = () => reject(new Error(`Unable to load chart renderer: ${path}`));
    document.head.append(script);
  });
}
async function runtime() {
  libraries ||= (async () => {
    await loadScript('./vega-6.2.0.min.js');
    await loadScript('./vega-lite-6.4.1.min.js');
    await loadScript('./vega-interpreter-2.2.1.min.js');
    Object.assign(window.vega, window.vegaInterpreter);
    if (!window.vega?.expressionInterpreter) throw new Error('Vega expression interpreter is unavailable');
  })().catch(error => { libraries = null; throw error; });
  return libraries;
}

export function prepareVega(value) {
  if (!value?.spec || !Array.isArray(value.table?.columns) || !Array.isArray(value.table?.data)) throw new Error('Custom chart requires a specification and Table');
  const spec = structuredClone(value.spec);
  function check(node) {
    if (!node || typeof node !== 'object') return;
    for (const [key, item] of Object.entries(node)) {
      if (['url', 'href'].includes(key)) throw new Error('Custom charts cannot load external resources or create links');
      if (['__proto__', 'prototype', 'constructor'].includes(key)) throw new Error('Invalid chart object key');
      check(item);
    }
  }
  check(spec);
  const rows = value.table.data.map(row => Object.fromEntries(value.table.columns.map((key, i) => [key, row[i]])));
  if (Array.isArray(spec.data)) {
    const entry = spec.data.find(data => data.name === 'wandb');
    if (entry) entry.values = rows;
    else spec.data.push({name: 'wandb', values: rows});
  } else {
    spec.datasets = {...spec.datasets, wandb: rows};
    spec.data ||= {name: 'wandb'};
  }
  spec.background ||= '#101820';
  spec.config = {axis: {labelColor:'#bdc9d5', titleColor:'#dce5ee', gridColor:'#263442'},
    legend:{labelColor:'#bdc9d5', titleColor:'#dce5ee'}, title:{color:'#dce5ee'}, ...spec.config};
  return spec;
}

/**
 * Purpose: Mount new charts and resize retained charts in the current layout.
 * Input: The view root and the chart registry.
 * Output: A promise for all chart dimensions to become available.
 * Note: Retained views keep their signal values and bound controls.
 */
export async function hydrateVega(root, registry) {
  for (const [node, view] of views) if (!node.isConnected) { view.finalize(); views.delete(node); }
  await Promise.all([...root.querySelectorAll('[data-vega-id]:not([data-vega-pending])')].map(async node => {
    if (!node.dataset.vegaReady) node.dataset.vegaPending = 'true';
    try {
      const value = registry.get(node.dataset.vegaId);
      if (node.dataset.vegaReady) {
        const view = views.get(node);
        // Vega-Lite container dimensions otherwise update only on window resize.
        if (value.spec.width === 'container') view.width(node.clientWidth);
        if (value.spec.height === 'container') view.height(node.clientHeight);
        await view.resize().runAsync();
        return;
      }
      const input = prepareVega(value);
      await runtime();
      if (!node.isConnected) return;
      const spec = input.mark || input.layer || input.hconcat || input.vconcat || input.facet
        ? window.vegaLite.compile(input).spec : input;
      const parsed = window.vega.parse(spec, null, {ast:true});
      const loader = {load: async () => { throw new Error('External chart data is disabled'); },
        sanitize: async () => { throw new Error('Chart resource links are disabled'); }};
      node.replaceChildren();
      const view = new window.vega.View(parsed, {expr:window.vega.expressionInterpreter,
        renderer:'svg', container:node, hover:true, loader});
      views.set(node, view);
      await view.runAsync();
      node.dataset.vegaReady = 'true';
    } catch (error) {
      node.textContent = `Chart could not be rendered: ${error.message}`;
      node.setAttribute('role', 'alert');
    } finally { delete node.dataset.vegaPending; }
  }));
}
