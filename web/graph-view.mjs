/**
 * Architecture: authenticated graph JSON -> stable node/edge layout -> inert SVG
 * and node inspector. Only declared edges are drawn. PyTorch module listings
 * remain unconnected; an execution order is not evidence of a dependency.
 * Main caller: app.mjs hydrateMedia. No external scripts, URLs or graph services.
 */
import {inspectGraphValue} from './graph-json.mjs';

export function graphLayout(graph) {
  if (!graph || !Array.isArray(graph.nodes) || !Array.isArray(graph.edges) ||
      graph.nodes.some(node => !node || typeof node !== 'object' || Array.isArray(node))) {
    throw new Error('This file does not contain a valid graph.');
  }
  const nodes = graph.nodes.map((value, index) => ({value, index, level:0}));
  const byId = new Map(nodes.map(node => [node.value.id, node]));
  const edges = graph.edges.map(edge => {
    if (!Array.isArray(edge) || edge.length !== 2 || !byId.has(edge[0]) || !byId.has(edge[1])) {
      throw new Error('A graph edge refers to a missing node.');
    }
    return {from:byId.get(edge[0]), to:byId.get(edge[1])};
  });
  const incoming = new Map(nodes.map(node => [node, 0])), outgoing = new Map(nodes.map(node => [node, []]));
  for (const edge of edges) { incoming.set(edge.to, incoming.get(edge.to) + 1); outgoing.get(edge.from).push(edge.to); }
  const queue = nodes.filter(node => !incoming.get(node));
  for (let i=0; i<queue.length; i++) for (const next of outgoing.get(queue[i])) {
    next.level = Math.max(next.level, queue[i].level + 1);
    incoming.set(next, incoming.get(next) - 1);
    if (!incoming.get(next)) queue.push(next);
  }
  const rows = new Map();
  nodes.forEach((node, i) => {
    if (!edges.length) { node.level = i % 3; node.row = Math.floor(i / 3); }
    else { node.row = rows.get(node.level) || 0; rows.set(node.level, node.row + 1); }
    node.x = 24 + node.level * 244; node.y = 24 + node.row * 104;
  });
  return {nodes, edges, width:Math.max(280, ...nodes.map(node => node.x + 232)),
    height:Math.max(120, ...nodes.map(node => node.y + 96))};
}

const element = (tag, cls, text) => {
  const node = document.createElement(tag); if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text; return node;
};
const svgElement = (tag, attributes) => {
  const node = document.createElementNS('http://www.w3.org/2000/svg', tag);
  for (const [key,value] of Object.entries(attributes)) node.setAttribute(key, value);
  return node;
};

/** Build a keyboard-accessible graph view from already authenticated bytes.
 * Main callers: media hydration and browser QA. Node names are always text;
 * selecting a node shows its complete attributes without truncating storage.
 */
export function graphViewer(graph, descriptor={}) {
  const layout = graphLayout(graph), root = element('div', 'graph-viewer');
  const toolbar = element('div','graph-toolbar');
  toolbar.append(element('span','tag',`${graph.format || 'Model'} · ${layout.nodes.length} nodes · ${layout.edges.length} edges`));
  const select = element('select'); select.setAttribute('aria-label','Inspect graph node');
  layout.nodes.forEach(node => { const option=element('option','',node.value.name ?? String(node.value.id ?? node.index)); option.value=node.index; select.append(option); });
  select.disabled = !layout.nodes.length; toolbar.append(select); root.append(toolbar);
  if (descriptor.capture && !descriptor.capture.complete) {
    const missing=descriptor.capture.missing_modules || [];
    root.append(element('p','notice graph-incomplete',`Incomplete capture: ${missing.length} modules have not executed${missing.length ? ': '+missing.join(', ') : '.'}`));
  }
  if (!layout.edges.length) root.append(element('p','table-note','Observed module listing. No dependency edges were recorded.'));
  if (!layout.nodes.length) {root.append(element('div','chart-empty','No modules observed yet.')); return root;}
  const viewport=element('div','graph-viewport'), svg=svgElement('svg',{
    viewBox:`0 0 ${layout.width} ${layout.height}`,width:layout.width,height:layout.height,
    role:'group','aria-label':'Model graph'});
  const inspect=element('pre','json-view graph-inspector'); inspect.setAttribute('aria-live','polite');
  const shapes=[];
  function show(index) {
    const selected=layout.nodes[index]; if(!selected)return;
    select.value=index; inspect.textContent=inspectGraphValue(selected.value);
    shapes.forEach((shape,i)=>shape.setAttribute('aria-pressed',String(i===index)));
  }
  for (const edge of layout.edges) {
    const x1=edge.from.x+208,y1=edge.from.y+36,x2=edge.to.x,y2=edge.to.y+36;
    svg.append(svgElement('path',{d:`M${x1},${y1} C${x1+32},${y1} ${x2-32},${y2} ${x2},${y2}`,class:'graph-edge'}));
    svg.append(svgElement('path',{d:`M${x2-7},${y2-4} L${x2},${y2} L${x2-7},${y2+4}`,class:'graph-arrow'}));
  }
  for (const node of layout.nodes) {
    const group=svgElement('g',{transform:`translate(${node.x},${node.y})`,class:'graph-node',role:'button',tabindex:'0','aria-label':node.value.name ?? String(node.value.id ?? node.index)});
    group.append(svgElement('rect',{width:208,height:72,rx:6}));
    const title=svgElement('title',{}); title.textContent=node.value.name ?? String(node.value.id ?? node.index); group.append(title);
    const name=svgElement('text',{x:12,y:25,class:'graph-name'}); name.textContent=String(node.value.name ?? node.value.id ?? node.index).slice(0,24); group.append(name);
    const detail=svgElement('text',{x:12,y:49,class:'graph-description'});
    detail.textContent=String(node.value.class_name || '').split('\n')[0].slice(0,28); group.append(detail);
    group.addEventListener('click',()=>show(node.index));
    group.addEventListener('keydown',event=>{if(event.key==='Enter'||event.key===' '){event.preventDefault();show(node.index);}});
    shapes.push(group); svg.append(group);
  }
  select.addEventListener('change',()=>show(Number(select.value)));
  viewport.append(svg);root.append(viewport,inspect);show(0);return root;
}
