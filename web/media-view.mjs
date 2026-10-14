/**
 * Architecture: authenticated image blob -> intrinsic image dimensions -> SVG
 * boxes and recolored class-mask canvases -> local layer/opacity controls.
 * Only the caller's authenticated blob resolver can load an annotation. Logged
 * text is inserted as text, never HTML. Images and masks keep their own pixels.
 * Main callers: app.mjs hydrateMedia and rerenders. Annotation failure preserves
 * the image. Matching media nodes survive rerenders, retaining playback and
 * local layer controls; changed descriptors get fresh players.
 */
import {decodeMaskPixels} from './mask-pixels.mjs';

const colors = ['#8abbff', '#6ac9d0', '#b49ae8', '#fbb360', '#4c90f0', '#ee8f9b', '#dc9fe2'];
export const classColor = id => colors[Math.abs(Number(id) || 0) % colors.length];

/** Convert official relative or pixel box positions into image coordinates.
 * Main callers: image overlay rendering and coordinate contract tests.
 */
export function boxGeometry(box, width, height) {
  const p = box.position || {}, relative = box.domain !== 'pixel';
  let x = p.minX, y = p.minY, w = p.maxX - p.minX, h = p.maxY - p.minY;
  if (Array.isArray(p.middle)) { w = p.width; h = p.height; x = p.middle[0] - w / 2; y = p.middle[1] - h / 2; }
  if (![x,y,w,h].every(Number.isFinite) || w < 0 || h < 0) return null;
  return { x: x * (relative ? width : 1), y: y * (relative ? height : 1), width: w * (relative ? width : 1), height: h * (relative ? height : 1) };
}

const element = (tag, className, text) => { const node = document.createElement(tag); if (className) node.className = className; if (text !== undefined) node.textContent = text; return node; };
const svgNode = (tag, attrs) => { const node = document.createElementNS('http://www.w3.org/2000/svg', tag); for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value); return node; };
const loadImage = (url, alt) => new Promise((resolve, reject) => { const image = new Image(); image.alt = alt || 'Logged image'; image.onload = () => resolve(image); image.onerror = () => reject(new Error('Image bytes could not be decoded.')); image.src = url; });
const pendingPlayback = new WeakMap();

/** Replace view markup while retaining players for the exact same logged value.
 * Main callers: workspace and run-detail renders. Reusing DOM nodes preserves
 * seek position, volume, speed and image-layer controls. Some browsers pause a
 * media node while it moves; resume only players that were already playing.
 */
export function replaceView(root, html) {
  const retained = new Map([...root.querySelectorAll('[data-media-key][data-media-ready]')].map(node => [node.dataset.mediaKey, node]));
  const template = document.createElement('template'); template.innerHTML = html;
  const players = [];
  for (const next of template.content.querySelectorAll('[data-media-key]')) {
    const prior = retained.get(next.dataset.mediaKey);
    if (!prior) continue;
    retained.delete(next.dataset.mediaKey);
    for (const player of prior.querySelectorAll('audio,video')) {
      const pending = pendingPlayback.get(player);
      if (pending) player.removeEventListener('loadedmetadata', pending.restore);
      players.push([player, pending?.state || { time: player.currentTime, playing: !player.paused && !player.ended, rate: player.playbackRate, volume: player.volume, muted: player.muted }]);
    }
    next.replaceChildren(...prior.childNodes); next.dataset.mediaReady = 'true';
  }
  const plots = new Map([...root.querySelectorAll('[data-plotly-key][data-plotly-ready]')].map(node=>[node.dataset.plotlyKey,node]));
  for (const next of template.content.querySelectorAll('[data-plotly-key]')) {
    const prior=plots.get(next.dataset.plotlyKey); if(!prior)continue;
    plots.delete(next.dataset.plotlyKey);next.replaceWith(prior);
  }
  for (const prior of root.querySelectorAll('[data-plotly-key]')) prior.dispatchEvent(new Event('backprop:dispose'));
  root.replaceChildren(template.content);
  for (const [player, state] of players) {
    const restore = () => {
      player.removeEventListener('loadedmetadata', restore);
      if (!player.isConnected) return;
      player.currentTime = state.time; player.playbackRate = state.rate; player.volume = state.volume; player.muted = state.muted;
      pendingPlayback.delete(player);
      if (state.playing) player.play().then(() => player.closest('[data-media-key]')?.querySelector('.playback-notice')?.remove()).catch(() => {
        const container = player.closest('[data-media-key]');
        if (container && !container.querySelector('.playback-notice')) container.append(element('p', 'notice playback-notice', 'Playback paused after refresh. Press Play to continue.'));
      });
    };
    pendingPlayback.set(player, { state, restore });
    player.addEventListener('loadedmetadata', restore, {once:true});
    if (player.readyState >= 1) restore();
  }
}

/** Surface browser decoding failures after a successful authenticated download.
 * Main caller: hydrateMedia for audio, video and animated GIF. Native controls
 * alone can fail silently; retain the original bytes as an explicit download.
 */
export function watchMediaErrors(player, container, value, url) {
  player.addEventListener('error', () => {
    const pending = pendingPlayback.get(player);
    if (pending) { player.removeEventListener('loadedmetadata', pending.restore); pendingPlayback.delete(player); }
    container = player.closest('[data-media-key]') || container;
    if (!container.isConnected || container.querySelector('.media-decode-error')) return;
    const notice = element('div', 'notice error media-decode-error'); notice.setAttribute('role', 'alert');
    notice.append(document.createTextNode(`This browser could not decode the logged ${value._type}. The file may be unsupported or damaged. `));
    const link = element('a', '', 'Download original file'); link.href = url;
    link.download = (value.path || value.artifact_path || `logged-${value._type}`).split('/').at(-1);
    notice.append(link); container.append(notice);
  });
}

/** Attach an image immediately, then enrich it with independent layers.
 * Main callers: hydrateMedia. Layer toggles change only this viewer, preserving
 * original stored media. A failed mask gets a visible error, not a blank image.
 */
export async function imageViewer(value, url, resolveMedia) {
  const image = await loadImage(url, value.caption), viewer = element('div', 'image-viewer');
  image.className = 'rich-media';
  const frame = element('div', 'annotation-frame'); frame.append(image); viewer.append(frame);
  const controls = element('div', 'annotation-controls'), layers = element('div', 'annotation-layers'); controls.append(layers);
  const width = image.naturalWidth, height = image.naturalHeight;
  const addLayer = (label, overlay, legend) => {
    const row = element('label', 'annotation-toggle'), check = element('input'); check.type = 'checkbox'; check.checked = true;
    check.addEventListener('change', () => { overlay.toggleAttribute('hidden', !check.checked); legend.hidden = !check.checked; });
    row.append(check, document.createTextNode(label)); layers.append(row); viewer.insertBefore(legend, viewer.querySelector('.image-caption'));
  };
  for (const [name, layer] of Object.entries(value.boxes || {})) {
    const overlay = svgNode('svg', { viewBox: `0 0 ${width} ${height}`, class: 'annotation-overlay', 'aria-label': `${name} bounding boxes` });
    const legend = element('div', 'annotation-legend');
    for (const box of layer.box_data || []) {
      const geometry = boxGeometry(box, width, height); if (!geometry) continue;
      const color = classColor(box.class_id), label = box.box_caption || layer.class_labels?.[box.class_id] || `Class ${box.class_id ?? '?'}`;
      const group = svgNode('g', {}), rect = svgNode('rect', { ...geometry, fill: 'none', stroke: color, 'stroke-width': 2, 'vector-effect': 'non-scaling-stroke' });
      const title = svgNode('title', {}); title.textContent = `${label}${box.scores ? ` · ${Object.entries(box.scores).map(([key, number]) => `${key}: ${number}`).join(', ')}` : ''}`;
      const text = svgNode('text', { x: geometry.x + width * .006, y: Math.max(height * .025, geometry.y - height * .008), fill: color, 'font-size': Math.max(10, width / 45), 'paint-order': 'stroke', stroke: '#101714', 'stroke-width': Math.max(1, width / 400) }); text.textContent = label;
      group.append(title, rect, text); overlay.append(group);
      const item = element('span', '', title.textContent); item.style.setProperty('--class-color', color); legend.append(item);
    }
    frame.append(overlay); addLayer(`${name} · boxes`, overlay, legend);
  }
  const masks = Object.entries(value.masks || {});
  if (Object.keys(value.boxes || {}).length || masks.length) viewer.insertBefore(controls, viewer.children[1] || null);
  if (masks.length) {
    const label = element('label', 'mask-opacity', 'Mask opacity '), opacity = element('input'); opacity.type = 'range'; opacity.min = '0'; opacity.max = '1'; opacity.step = '.05'; opacity.value = '.45'; opacity.setAttribute('aria-label', 'Mask opacity'); label.append(opacity); controls.append(label);
    opacity.addEventListener('input', () => { for (const canvas of frame.querySelectorAll('canvas')) canvas.style.opacity = opacity.value; });
    // Each mask can fail independently. A diagnostic remains beside the image.
    void Promise.all(masks.map(async ([name, layer]) => {
      try {
        const mask = await decodeMaskPixels(await resolveMedia(layer.mask)), canvas = element('canvas', 'annotation-overlay'); canvas.width = mask.width; canvas.height = mask.height; canvas.style.opacity = opacity.value; canvas.setAttribute('aria-label', `${name} segmentation mask`);
        const context = canvas.getContext('2d'), pixels = new ImageData(mask.data, mask.width, mask.height), present = new Set();
        for (let i = 0; i < pixels.data.length; i += 4) { const id = pixels.data[i]; if (!pixels.data[i + 3]) continue; present.add(id); const color = classColor(id); pixels.data[i] = parseInt(color.slice(1,3),16); pixels.data[i+1] = parseInt(color.slice(3,5),16); pixels.data[i+2] = parseInt(color.slice(5,7),16); pixels.data[i+3] = 255; }
        context.putImageData(pixels, 0, 0); frame.append(canvas);
        const legend = element('div', 'annotation-legend'); for (const id of [...present].sort((a,b) => a-b)) { const item = element('span', '', `${layer.class_labels?.[id] || `Class ${id}`}`); item.style.setProperty('--class-color', classColor(id)); legend.append(item); }
        addLayer(`${name} · mask`, canvas, legend);
      } catch (error) { viewer.append(element('div', 'notice error', `Could not load mask ${name}: ${error.message}`)); }
    }));
  }
  const footer = element('div', 'image-caption'); footer.append(element('span', '', value.caption || 'Logged image'), element('span', 'dim', `${width} × ${height}${value.grouping ? ` · ${value.grouping}` : ''}`)); viewer.append(footer);
  return viewer;
}
