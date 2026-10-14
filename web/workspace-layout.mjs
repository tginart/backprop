/**
 * Architecture: local project layout -> stable sections and panel order -> DOM
 * handles -> pointer/keyboard resize or drag -> commit preference changes.
 * Main callers: app workspace rendering and saved-view serialization.
 * Invariants: display edits never rename a logged metric; custom section IDs
 * stay stable when titles change; each panel identity occurs in exactly one section; copies share metric data.
 */
export function newLayout() {
  return {sidebarWidth:250, sidebarHidden:false, pinnedColumns:[], pinnedRuns:[], runPageSize:20, panelOrder:[], panelOptions:[], sectionOrder:[], sections:[], copies:[]};
}
export const defaultSection = metric => metric.includes('/') ? metric.split('/')[0] : 'Metrics';
export const panelOptions = (layout, metric) => new Map(layout.panelOptions).get(metric) || {};
export function setPanelOptions(layout, metric, changes) {
  const entries = new Map(layout.panelOptions);
  entries.set(metric, {...entries.get(metric),...changes});
  layout.panelOptions = [...entries];
}
export const sectionOptions = (layout, id) => layout.sections.find(section => section.id === id) || {id,title:id,settings:{}};
export function setSection(layout, id, changes) {
  const index = layout.sections.findIndex(section => section.id === id);
  const section = {...sectionOptions(layout,id),...changes};
  if (index === -1) layout.sections.push(section); else layout.sections[index] = section;
}

/** Put saved order first while retaining newly arriving metrics and sections.
 * Main callers: arrangePanels/movePanel. Unknown saved items do not hide data.
 */
export function ordered(items, order) {
  const ranks = new Map(order.map((key,index) => [key,index]));
  return items.slice().sort((a,b) => (ranks.get(a) ?? Infinity)-(ranks.get(b) ?? Infinity));
}
export function arrangePanels(metrics, layout, pinned = new Set(), sourceFor = metric => metric) {
  const sections = new Map(layout.sections.filter(section => section.custom).map(section => [section.id,[]]));
  for (const metric of ordered(metrics,layout.panelOrder)) {
    const id = pinned.has(metric) ? 'Pinned' : panelOptions(layout,metric).section || defaultSection(sourceFor(metric));
    if (!sections.has(id)) sections.set(id,[]);
    sections.get(id).push(metric);
  }
  const base = [...sections.keys()].sort((a,b) => {
    const rank = id => id === 'Pinned' ? -1 : /^(train|training)$/.test(id) ? 0 : /^(eval|validation|test)$/.test(id) ? 1 : id === 'Metrics' ? 3 : /^(system|performance)$/.test(id) ? 4 : 2;
    return rank(a)-rank(b) || a.localeCompare(b);
  });
  return ordered(base,layout.sectionOrder).sort((a,b)=>Number(!!sectionOptions(layout,b).pinned)-Number(!!sectionOptions(layout,a).pinned)).filter(id=>!sectionOptions(layout,id).hidden).map(id => ({...sectionOptions(layout,id),metrics:sections.get(id)}));
}

/** Move a metric before an existing panel or to the end of a chosen section.
 * Main callers: drag/drop and accessible panel layout editor.
 */
export function movePanel(layout, metrics, metric, section, before = null) {
  if (before === metric && (panelOptions(layout,metric).section || defaultSection(metric)) === section) return;
  if (!metrics.includes(metric)) throw new Error('That metric is no longer available.');
  const order = ordered(metrics,layout.panelOrder).filter(key => key !== metric);
  const index = before ? order.indexOf(before) : -1;
  index < 0 ? order.push(metric) : order.splice(index,0,metric);
  layout.panelOrder = order;
  setPanelOptions(layout,metric,{section});
}

/** Bind pointer resizing and native HTML drag/drop after rendering the canvas.
 * Main callers: app.render. Pointer drafts update styles without rebuilding the
 * DOM; only pointerup commits. Cancellation restores the previous dimensions.
 * Arrow keys provide the same sidebar and panel resizing without a pointer.
 */
export function bindWorkspaceLayout(root, {layout, columns, onCommit, onMove, onSectionMove}) {
  const sidebar = root.querySelector('[data-sidebar-resize]'), workspace = root.querySelector('.workspace');
  function bindResize(handle, initial, apply, commit, measure) {
    let drag = null;
    handle.addEventListener('pointerdown', event => {
      if (event.button !== 0) return;
      drag = {x:event.clientX,y:event.clientY,value:initial()}; handle.setPointerCapture(event.pointerId);
      handle.classList.add('resizing'); event.preventDefault();
    });
    handle.addEventListener('pointermove', event => { if (drag) apply(measure(drag,event)); });
    handle.addEventListener('pointerup', event => {
      if (!drag) return;
      const value = measure(drag,event); drag = null; handle.classList.remove('resizing'); apply(value); commit(value);
    });
    handle.addEventListener('pointercancel', () => { if (drag) apply(drag.value); drag = null; handle.classList.remove('resizing'); });
    handle.addEventListener('keydown', event => {
      if (!['ArrowLeft','ArrowRight','ArrowUp','ArrowDown'].includes(event.key)) return;
      event.preventDefault();
      const value = initial(), delta = event.key === 'ArrowLeft' || event.key === 'ArrowUp' ? -1 : 1;
      const next = typeof value === 'number' ? Math.max(190,Math.min(540,value+delta*16))
        : {span:Math.max(1,Math.min(columns,value.span+(event.key.includes('Left') || event.key.includes('Right') ? delta : 0))),height:Math.max(230,Math.min(700,value.height+(event.key.includes('Up') || event.key.includes('Down') ? delta*20 : 0)))};
      apply(next); commit(next);
    });
  }
  if (sidebar && workspace) bindResize(sidebar, () => layout.sidebarWidth,
    value => {workspace.style.setProperty('--run-width',`${value}px`); sidebar.setAttribute('aria-valuenow',String(value));},
    value => {layout.sidebarWidth=value;onCommit();},
    (start,event) => Math.round(Math.max(190,Math.min(540,start.value+event.clientX-start.x))));
  for (const handle of root.querySelectorAll('[data-panel-resize]')) {
    const metric = handle.dataset.panelResize, card = handle.closest('.chart-card'), grid = card.closest('.chart-grid');
    bindResize(handle, () => ({span:Math.min(columns,panelOptions(layout,metric).span || 1),height:panelOptions(layout,metric).height || 270}),
      value => {card.style.setProperty('--panel-span',value.span);card.style.setProperty('--panel-height',`${value.height}px`);},
      value => {setPanelOptions(layout,metric,value);onCommit();},
      (start,event) => ({span:Math.max(1,Math.min(columns,start.value.span+Math.round((event.clientX-start.x)/(grid.clientWidth/columns)))),height:Math.round(Math.max(230,Math.min(700,start.value.height+event.clientY-start.y)))}));
  }
  let dragged = null;
  for (const handle of root.querySelectorAll('[data-panel-drag],[data-section-drag]')) {
    handle.addEventListener('dragstart', event => {
      dragged = handle.dataset.panelDrag ? {metric:handle.dataset.panelDrag} : {section:handle.dataset.sectionDrag};
      event.dataTransfer.effectAllowed = 'move'; event.dataTransfer.setData('text/plain',JSON.stringify(dragged));
      handle.closest('.chart-card,.metric-section').classList.add('dragging');
    });
    handle.addEventListener('dragend', () => {dragged=null;for(const node of root.querySelectorAll('.dragging,.drop-target')) node.classList.remove('dragging','drop-target');});
  }
  for (const zone of root.querySelectorAll('[data-section-id],.chart-card[data-metric]')) {
    zone.addEventListener('dragover', event => {if(!dragged)return;event.preventDefault();event.stopPropagation();zone.classList.add('drop-target');});
    zone.addEventListener('dragleave', () => zone.classList.remove('drop-target'));
    zone.addEventListener('drop', event => {
      if(!dragged)return;event.preventDefault();event.stopPropagation();zone.classList.remove('drop-target');
      const section = zone.closest('[data-section-id]')?.dataset.sectionId;
      if(dragged.metric && section) onMove(dragged.metric,section,zone.dataset.metric || null);
      if(dragged.section && section) onSectionMove(dragged.section,section);
      dragged=null;
    });
  }
}
