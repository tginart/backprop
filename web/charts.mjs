/**
 * Architecture: numeric series -> domains -> SVG -> local pointer interaction.
 * The renderer is dependency free and makes no network calls. Hover uses the
 * recorded points near the cursor and interpolates between them elsewhere.
 * Dense paths keep min/max points in each bucket so spikes
 * remain visible. Zoom changes only displayed axis ranges, never stored data.
 * Main callers: app.mjs metric and rich-data panels.
 */
import { escapeHtml as e, finite, formatNumber as f } from './model.mjs';

const W = 600, H = 250, L = 76, R = 18, T = 18, B = 32;
// Optional axis titles reserve their own space. With no labels, these preserve
// the original plot geometry used by existing pointer and keyboard inspection.
const plotLeft = spec => L + (spec.yLabel ? 24 : 0);
const bottomMargin = spec => B + (spec.xLabel ? 16 : 0);
const plotWidth = spec => W - plotLeft(spec) - R;
const plotHeight = spec => H - T - bottomMargin(spec);
/** Choose enough precision to distinguish every recorded tick in a domain.
 * Main callers: renderChart for numeric X/Y axes. A fixed two-digit label can
 * make distinct accuracy values or a zoomed range look identical.
 */
export function axisTickLabels(values) {
  const distinct = new Set(values).size;
  for (let precision = 2; precision <= 17; precision++) {
    const labels = values.map(number => Math.abs(number) >= 1000
      ? `${Number((number / 1000).toPrecision(Math.max(3,precision)))}k`
      : Math.abs(number) < .01 && number !== 0
        ? number.toExponential(precision - 1) : String(Number(number.toPrecision(precision))));
    if (new Set(labels).size === distinct || precision === 17) return labels;
  }
}

export function decimate(points, limit = 700) {
  if (points.length <= limit) return points;
  const size = Math.ceil(points.length / (limit / 2));
  const out = [points[0]];
  for (let i = 1; i < points.length - 1; i += size) {
    const bucket = points.slice(i, Math.min(i + size, points.length - 1));
    let lo = bucket[0], hi = bucket[0];
    for (const point of bucket) { if (point.y < lo.y) lo = point; if (point.y > hi.y) hi = point; }
    out.push(...(lo.x <= hi.x ? [lo, hi] : [hi, lo]));
  }
  out.push(points.at(-1));
  return out;
}

export function chartSpec(series, options = {}) {
  const valid = series.map(s => {
    let gap = false;
    const points = [];
    for (const point of s.points) {
      if (!finite(point.x) || !finite(point.y) || options.logScale && point.y <= 0 || options.logX && point.x <= 0) { gap = true; continue; }
      points.push(gap ? {...point, breakBefore:true} : point); gap = false;
    }
    return {...s, points};
  });
  const all = valid.flatMap(s => s.points);
  if (!all.length) return null;
  const halfWidth = p => options.kind === 'bar' ? (p.width || 1) / 2 : 0;
  const extents = (p, axis) => [p[axis], ...(p[axis === 'x' ? 'errorX' : 'errorY'] || []), ...(axis === 'y' ? p.band || [] : []), ...(axis === 'y' && options.smoothing && options.showRaw !== false ? [p.raw] : [])].filter(value => finite(value) && (!(axis === 'x' ? options.logX : options.logScale) || value > 0));
  const xMin = options.zoom?.[0] ?? options.xMin ?? all.reduce((min, p) => Math.min(min, ...extents(p, 'x').map(x => x + (p.offset || 0) - halfWidth(p))), Infinity);
  const xMax = options.zoom?.[1] ?? options.xMax ?? all.reduce((max, p) => Math.max(max, ...extents(p, 'x').map(x => x + (p.offset || 0) + halfWidth(p))), -Infinity);
  const shown = all.filter(p => p.x >= xMin && p.x <= xMax);
  const yy = (shown.length ? shown : all).flatMap(p => extents(p, 'y').map(y => options.logScale ? Math.log10(y) : y));
  let yMin = yy.reduce((min, y) => Math.min(min, y), Infinity), yMax = yy.reduce((max, y) => Math.max(max, y), -Infinity);
  if (options.kind === 'bar' && !options.logScale) { yMin = Math.min(0, yMin); yMax = Math.max(0, yMax); }
  const pad = yMin === yMax ? Math.max(Math.abs(yMin) * 0.1, 0.1) : (yMax - yMin) * 0.08;
  yMin -= pad; yMax += pad;
  if (finite(options.yMin)) yMin = options.logScale ? Math.log10(options.yMin) : options.yMin;
  if (finite(options.yMax)) yMax = options.logScale ? Math.log10(options.yMax) : options.yMax;
  if (finite(options.zoom?.[2])) yMin = options.logScale ? Math.log10(options.zoom[2]) : options.zoom[2];
  if (finite(options.zoom?.[3])) yMax = options.logScale ? Math.log10(options.zoom[3]) : options.zoom[3];
  if (![xMin, xMax, yMin, yMax].every(finite) || xMin > xMax || yMin >= yMax || options.logX && xMin <= 0) return null;
  return { ...options, series: valid, xMin, xMax: xMax === xMin ? options.logX ? xMin * 10 : xMin + 1 : xMax, yMin, yMax };
}

const sx = (value, spec) => plotLeft(spec) + (spec.logX ? (Math.log10(value) - Math.log10(spec.xMin)) / (Math.log10(spec.xMax) - Math.log10(spec.xMin)) : (value - spec.xMin) / (spec.xMax - spec.xMin)) * plotWidth(spec);
const sy = (value, spec) => T + (1 - ((spec.logScale ? Math.log10(value) : value) - spec.yMin) / (spec.yMax - spec.yMin)) * plotHeight(spec);
const xValue = (fraction, spec) => spec.logX ? 10 ** (Math.log10(spec.xMin) + fraction * (Math.log10(spec.xMax) - Math.log10(spec.xMin))) : spec.xMin + fraction * (spec.xMax - spec.xMin);
const path = (points, spec, raw = false) => {
  const segments = [];
  for (const point of points) { if (!segments.length || point.breakBefore) segments.push([]); segments.at(-1).push(raw ? {...point, y:point.raw ?? point.y} : point); }
  return segments.map(segment => decimate(segment).map((p, i) => `${i ? 'L' : 'M'}${sx(p.x, spec).toFixed(2)},${sy(p.y, spec).toFixed(2)}`).join(' ')).join(' ');
};

const lowerBound = (values, target, key = value => value) => {
  let lo = 0, hi = values.length;
  while (lo < hi) { const mid = (lo + hi) >> 1; if (key(values[mid]) < target) lo = mid + 1; else hi = mid; }
  return lo;
};

/** A small screen-space snap zone leaves most of each segment freely readable.
 * Main caller: pointer inspection. Dense samples cap their attraction to a fifth
 * of the adjacent spacing; the radius is supplied in SVG units after scaling.
 */
export function snapChartX(xs, x, spec, radius = 6) {
  const index = lowerBound(xs, valueAtPlotX(x, spec));
  const candidates = [index - 1, index].filter(i => i >= 0 && i < xs.length);
  const nearest = candidates.sort((a,b) => Math.abs(sx(xs[a],spec)-x)-Math.abs(sx(xs[b],spec)-x))[0];
  if (nearest === undefined) return x;
  const position = sx(xs[nearest],spec);
  const spacing = Math.min(...[nearest-1,nearest+1].filter(i=>i>=0 && i<xs.length).map(i=>Math.abs(sx(xs[i],spec)-position)));
  return Math.abs(position-x) <= Math.min(radius,spacing*.2) ? position : x;
}
const valueAtPlotX = (x, spec) => xValue((x - plotLeft(spec)) / plotWidth(spec), spec);

/** Sample the drawn linear segment without inventing points across gaps.
 * Main caller: bindChart. Points must be sorted by X; nonmonotonic source paths
 * opt out of interpolation. Log axes interpolate in their displayed space.
 * Derived samples carry no recorded counts, error bars or uncertainty bands.
 */
export function chartSample(series, target, spec = {}) {
  const index = lowerBound(series.points, target, point => point.x);
  const before = series.points[index-1], after = series.points[index];
  const nearest = [before,after].filter(Boolean).sort((a,b)=>Math.abs(a.x-target)-Math.abs(b.x-target))[0];
  if (!nearest) return null;
  const recorded = point => ({...series, point, interpolated:false});
  if (Math.abs(nearest.x-target) <= Number.EPSILON*8*Math.max(1,Math.abs(target),Math.abs(nearest.x))) return recorded(nearest);
  const kind = series.kind || spec.kind || 'line';
  if (!['line','area'].includes(kind) || series.interpolate === false) return recorded(nearest);
  if (!before || !after || after.breakBefore) return null;
  const fraction = spec.logX ? Math.log(target/before.x)/Math.log(after.x/before.x) : (target-before.x)/(after.x-before.x);
  const mix = (a,b) => spec.logScale ? 10 ** (Math.log10(a)+(Math.log10(b)-Math.log10(a))*fraction) : a+(b-a)*fraction;
  const point = {x:target, y:mix(before.y,after.y)};
  if (finite(before.raw) && finite(after.raw) && (!spec.logScale || before.raw > 0 && after.raw > 0)) point.raw = mix(before.raw,after.raw);
  return {...series, point, interpolated:true};
}

/** Draw uncertainty at its actual data endpoints, clipped with the series.
 * Main callers: renderChart. Domain calculation above includes these endpoints;
 * a nonpositive lower endpoint on a log axis clips at the visible minimum.
 */
function errorBars(points, series, spec) {
  const shapes = points.map(p => {
    const x = sx(p.x + (p.offset || 0), spec), y = sy(p.y, spec), cap = 4;
    let shape = '';
    if (p.errorY) { const [lo, hi] = p.errorY.map(value => sy(spec.logScale ? Math.max(10 ** spec.yMin, value) : value, spec)); shape += `<path class="error-bar-y" d="M${x},${lo}V${hi}M${x-cap},${lo}H${x+cap}M${x-cap},${hi}H${x+cap}"/>`; }
    if (p.errorX) { const [lo, hi] = p.errorX.map(value => sx((spec.logX ? Math.max(spec.xMin, value) : value) + (p.offset || 0), spec)); shape += `<path class="error-bar-x" d="M${lo},${y}H${hi}M${lo},${y-cap}V${y+cap}M${hi},${y-cap}V${y+cap}"/>`; }
    return shape;
  }).join('');
  return shapes ? `<g class="error-bars" stroke="${series.color}" stroke-width="1.2">${shapes}</g>` : '';
}

/** Draw the recorded group range as a ribbon, including gaps and log clipping.
 * Main callers: renderChart. Each ribbon stays inside its continuous segment;
 * no band is fabricated for a point without an aggregate range.
 */
function variationBand(points, series, spec) {
  const segments = [];
  let segment = null;
  for (const point of points) {
    if (!point.band?.every(finite) || point.band[0] > point.band[1] || spec.logScale && point.band[1] <= 0) { segment = null; continue; }
    if (!segment || point.breakBefore) { segment = []; segments.push(segment); }
    segment.push(point);
  }
  return segments.filter(segment => segment.length > 1).map(segment => {
    const boundary = (point, end) => `${sx(point.x,spec).toFixed(2)},${sy(spec.logScale ? Math.max(10 ** spec.yMin,point.band[end]) : point.band[end],spec).toFixed(2)}`;
    const d = `M${segment.map(point => boundary(point,1)).join(' L')} L${segment.slice().reverse().map(point => boundary(point,0)).join(' L')} Z`;
    return `<path class="variation-band" d="${d}" fill="${series.color}" fill-opacity=".16" stroke="none"/>`;
  }).join('');
}

export function renderChart(spec, id = 'chart') {
  if (!spec) return '<div class="chart-empty">No numeric points for this view.</div>';
  const left = plotLeft(spec), bottom = bottomMargin(spec), width = plotWidth(spec), height = plotHeight(spec);
  const yValues = Array.from({length:5},(_,i) => spec.yMax - i / 4 * (spec.yMax - spec.yMin));
  const yLabels = axisTickLabels(yValues.map(value => spec.logScale ? 10 ** value : value));
  const grid = yValues.map((value, i) => {
    const y = T + i / 4 * height;
    return `<line x1="${left}" y1="${y}" x2="${W - R}" y2="${y}" class="grid-line"/><text data-tick="${i}" x="${left - 11}" y="${y + 4}" text-anchor="end">${e(yLabels[i])}</text>`;
  }).join('');
  const categoryTicks = spec.categories?.map((label, value) => ({ label, value })).filter(item => item.value >= spec.xMin && item.value <= spec.xMax);
  const tickValues = categoryTicks ? categoryTicks.filter((_, index) => categoryTicks.length <= 5 || index % Math.ceil((categoryTicks.length - 1) / 4) === 0 || index === categoryTicks.length - 1)
    : Array.from({ length: 5 }, (_, i) => ({ value: xValue(i / 4, spec) }));
  const xLabels = axisTickLabels(tickValues.map(item => item.value));
  const ticks = tickValues.map(({ value, label }, i) => {
    label ??= spec.timeAxis ? new Date(value * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : xLabels[i];
    return `<text data-tick="${Math.round(i * 4 / Math.max(1, tickValues.length - 1))}" x="${sx(value, spec)}" y="${spec.xLabel ? H - bottom + 17 : H - 9}" text-anchor="middle">${e(String(label).slice(0, 18))}</text>`;
  }).join('');
  const labelText = (label, limit) => { const text = String(label); return e(text.length > limit ? text.slice(0, limit - 1) + '…' : text); };
  const axisTitles = `${spec.xLabel ? `<text class="chart-axis-label x-axis-label" x="${left + width / 2}" y="${H - 5}" text-anchor="middle" style="font-size:13px"><title>${e(spec.xLabel)}</title>${labelText(spec.xLabel, 60)}</text>` : ''}${spec.yLabel ? `<text class="chart-axis-label y-axis-label" transform="translate(17 ${T + height / 2}) rotate(-90)" text-anchor="middle" style="font-size:13px"><title>${e(spec.yLabel)}</title>${labelText(spec.yLabel, 24)}</text>` : ''}`;
  const shapes = spec.series.map(series => {
    const points = series.points;
    const errors = errorBars(points, series, spec);
    if (series.kind === 'none') return errors;
    if (spec.kind === 'bar') return points.map((p, index) => {
      const barWidth = p.width ? p.width / (spec.xMax - spec.xMin) * width : Math.max(2, width / Math.max(points.length, 1) * 0.7);
      const y = sy(p.y, spec), zero = Math.min(H - bottom, sy(spec.logScale ? 10 ** spec.yMin : 0, spec));
      return `<rect x="${sx(p.x + (p.offset || 0), spec) - barWidth / 2}" y="${Math.min(y, zero)}" width="${barWidth}" height="${Math.max(1, Math.abs(zero - y))}" fill="${series.color}" opacity=".72" rx="1"/>`;
    }).join('') + errors;
    const markers = (series.markers || (series.kind || spec.kind) === 'scatter') ? decimate(points, 1000).map(p => `<circle cx="${sx(p.x, spec)}" cy="${sy(p.y, spec)}" r="${(series.markerSize || 6) / 2}" fill="${series.color}" opacity=".72"/>`).join('') : '';
    if ((series.kind || spec.kind) === 'scatter') return markers + errors;
    const raw = spec.smoothing && spec.showRaw !== false ? `<path class="chart-raw-line" d="${path(points, spec, true)}" stroke="${series.color}" stroke-width="1" opacity=".17"/>` : '';
    const fill = spec.kind === 'area' && points.length ? `<path d="${path(points, spec)} L${sx(points.at(-1).x, spec)},${H - bottom} L${sx(points[0].x, spec)},${H - bottom} Z" fill="${series.color}" opacity=".12" stroke="none"/>` : '';
    return `${variationBand(points,series,spec)}${fill}${raw}<path class="chart-line" d="${path(points, spec)}" stroke="${series.color}" stroke-width="1.8" ${series.dashed ? 'stroke-dasharray="5 3"' : ''}/>${markers}${errors}${points.length === 1 && !markers ? `<circle cx="${sx(points[0].x, spec)}" cy="${sy(points[0].y, spec)}" r="3" fill="${series.color}"/>` : ''}`;
  }).join('');
  return `<svg class="chart-svg" viewBox="0 0 ${W} ${H}" role="img" tabindex="0" aria-label="${e(spec.title || 'Metric plot')}.${spec.xLabel ? ` X axis: ${e(spec.xLabel)}.` : ''}${spec.yLabel ? ` Y axis: ${e(spec.yLabel)}.` : ''} Arrow keys inspect points. Drag to zoom, double click or press zero to reset."><defs><clipPath id="clip-${e(id)}"><rect x="${left}" y="${T}" width="${width}" height="${height}"/></clipPath></defs><g class="axis">${grid}${ticks}${axisTitles}</g><g clip-path="url(#clip-${e(id)})" fill="none" stroke-linejoin="round" stroke-linecap="round">${shapes}<rect class="zoom-selection" y="${T}" height="${height}" fill="#8abbff" opacity=".13" visibility="hidden"/><line class="crosshair" y1="${T}" y2="${H - bottom}" stroke="#839089" stroke-dasharray="3 3" visibility="hidden"/><g class="inspection-points" pointer-events="none"></g></g></svg><div class="chart-tooltip" role="status" popover="manual" tabindex="0" hidden></div>`;
}

/** Render hover values with an explicit count and reachable additional series.
 * Main callers: bindChart.inspect and chart inspection tests. The initial list
 * stays compact; native details exposes every remaining value without changing
 * plotted visibility or confusing this inspection with legend toggling.
 */
export function renderChartTooltip(spec, target, nearest, {expanded = false} = {}) {
  const limit = 12;
  const xText = value => spec.timeAxis ? new Date(value * 1000).toLocaleString() : f(value);
  const rows = samples => samples.map(s => `<div class="tooltip-series" style="display:flex;align-items:center;gap:7px;padding:3px 0"><span class="dot" style="background:${e(s.color)}"></span><span title="${e(s.name)}">${e(s.name)}</span><b>${e(f(s.point.y))}</b></div><small class="tooltip-sample">X ${e(xText(s.point.x))}${s.interpolated ? ' · Interpolated' : ' · Recorded point'}${s.point.count ? ` · ${s.point.count} runs · σ ${e(f(s.point.stddev))}` : ''}${s.point.raw !== undefined && s.point.raw !== s.point.y ? ` · raw ${e(f(s.point.raw))}` : ''}</small>`).join('');
  const count = nearest.length > limit ? `<small data-tooltip-series-count style="display:block;margin-bottom:7px;color:var(--muted)">${expanded ? nearest.length : limit} of ${nearest.length} series shown</small>` : '';
  const extra = nearest.length > limit ? `<details class="tooltip-additional-series" ${expanded ? 'open' : ''} style="margin-top:8px;border-top:1px solid var(--border);padding-top:7px"><summary style="cursor:pointer;color:var(--accent)">${expanded ? 'Hide' : 'Show'} ${nearest.length - limit} more series</summary>${rows(nearest.slice(limit))}</details>` : '';
  return `<div class="tooltip-title">${e(spec.xLabel || 'Step')} ${e(spec.categories ? spec.categories[Math.round(target)] ?? '' : xText(target))}</div>${count}${rows(nearest.slice(0, limit))}${extra}${nearest.length ? '' : '<small>No data at this X</small>'}`;
}

/** Attach pointer hover and drag selection after inserting the SVG.
 * Main callers: app.mjs bindCharts(). Zoom callback persists per-panel range.
 */
export function bindChart(element, spec, onZoom) {
  if (!spec) return;
  const svg = element.querySelector('svg'), tooltip = element.querySelector('.chart-tooltip');
  const crosshair = svg.querySelector('.crosshair'), selection = svg.querySelector('.zoom-selection');
  const inspectionPoints = svg.querySelector('.inspection-points');
  const left = plotLeft(spec), bottom = bottomMargin(spec), width = plotWidth(spec), height = plotHeight(spec);
  let start = null, keyboardIndex = -1, hoverFrame = 0, hideTimer = null, showAllSeries = false, tooltipAnchor = null, inspectionCount = 0;
  // The browser top layer escapes panel overflow and modal stacking contexts.
  // Keep ownership inside this chart so replacing a panel also removes its hover.
  const hideTooltip = () => {
    clearTimeout(hideTimer); showAllSeries = false;
    cancelAnimationFrame(hoverFrame);
    if (tooltip.matches(':popover-open')) tooltip.hidePopover();
    tooltip.hidden = true;
    crosshair.setAttribute('visibility', 'hidden');
    inspectionPoints.replaceChildren();
  };
  // A short gap between the SVG and floating tooltip must not prevent reaching
  // its scroll area or Show more control. Focus departure still closes at once.
  const deferHide = () => { clearTimeout(hideTimer); hideTimer = setTimeout(hideTooltip, 120); };
  const placeTooltip = (clientX, clientY) => {
    tooltip.hidden = false;
    if (!tooltip.matches(':popover-open')) tooltip.showPopover();
    const viewport = window.visualViewport;
    const left = viewport?.offsetLeft || 0, top = viewport?.offsetTop || 0;
    const width = viewport?.width || innerWidth, height = viewport?.height || innerHeight;
    tooltip.style.maxWidth = `${Math.max(0, width - 16)}px`;
    tooltip.style.maxHeight = `${Math.max(0, height - 16)}px`;
    const box = tooltip.getBoundingClientRect();
    const x = clientX + 12 + box.width <= left + width - 8 ? clientX + 12 : clientX - box.width - 12;
    tooltip.style.left = `${Math.max(left + 8, Math.min(x, left + width - box.width - 8))}px`;
    tooltip.style.top = `${Math.max(top + 8, Math.min(clientY + 12, top + height - box.height - 8))}px`;
    cancelAnimationFrame(hoverFrame);
    const initial = svg.getBoundingClientRect();
    const watchLayout = () => {
      const current = svg.getBoundingClientRect();
      if (!svg.isConnected || current.top !== initial.top || current.left !== initial.left ||
          current.width !== initial.width || current.height !== initial.height ||
          (viewport?.width || innerWidth) !== width || (viewport?.height || innerHeight) !== height) {
        hideTooltip(); return;
      }
      hoverFrame = requestAnimationFrame(watchLayout);
    };
    hoverFrame = requestAnimationFrame(watchLayout);
  };
  // Pointer hover and zoom share the browser's actual SVG transform. Resized
  // panels can leave margins around the viewBox, so rectangle ratios are wrong.
  const plotPosition = event => {
    const point = new DOMPoint(event.clientX, event.clientY).matrixTransform(svg.getScreenCTM().inverse());
    return { x: Math.max(left, Math.min(W - R, point.x)), y: Math.max(T, Math.min(H - bottom, point.y)) };
  };
  const value = x => xValue((x - left) / width, spec);
  const orderedSeries = spec.series.map(series => ({ ...series, interpolate:series.points.every((point,i)=>!i || point.x>=series.points[i-1].x), points: series.points.slice().sort((a,b) => a.x-b.x) }));
  const xs = [...new Set(orderedSeries.flatMap(series => series.points.filter(point => point.x >= spec.xMin && point.x <= spec.xMax).map(point => point.x)))].sort((a,b)=>a-b);
  const inspect = (x, clientX, clientY) => {
    const target = value(x);
    const nearest = orderedSeries.map(series => chartSample(series,target,spec)).filter(Boolean);
    inspectionCount = nearest.length;
    crosshair.setAttribute('x1', x); crosshair.setAttribute('x2', x); crosshair.setAttribute('visibility', 'visible');
    inspectionPoints.innerHTML = nearest.filter(sample=>!sample.interpolated && (sample.kind || spec.kind) !== 'bar' && sample.kind !== 'none').map(sample=>`<circle class="inspection-point" cx="${sx(sample.point.x,spec)}" cy="${sy(sample.point.y,spec)}" r="3.5" fill="${e(sample.color)}" stroke="var(--surface, #1c2127)" stroke-width="1.5"/>`).join('');
    tooltip.innerHTML = renderChartTooltip(spec, target, nearest, {expanded:showAllSeries});
    tooltipAnchor = {x:clientX, y:clientY};
    placeTooltip(clientX, clientY);
  };
  tooltip.addEventListener('toggle', event => {
    if (!event.target.matches('.tooltip-additional-series') || tooltip.hidden) return;
    showAllSeries = event.target.open;
    const count = tooltip.querySelector('[data-tooltip-series-count]'), total = inspectionCount;
    count.textContent = `${showAllSeries ? total : 12} of ${total} series shown`;
    event.target.querySelector('summary').textContent = `${showAllSeries ? 'Hide' : 'Show'} ${total - 12} more series`;
    if (tooltipAnchor) placeTooltip(tooltipAnchor.x, tooltipAnchor.y);
  }, true);
  svg.addEventListener('pointerenter', () => clearTimeout(hideTimer));
  tooltip.addEventListener('pointerenter', () => clearTimeout(hideTimer));
  svg.addEventListener('pointermove', event => {
    const { x, y } = plotPosition(event);
    if (start !== null) {
      const horizontalOnly = Math.abs(y - start.y) <= 8, verticalOnly = Math.abs(x - start.x) <= 8;
      selection.setAttribute('x', verticalOnly ? left : Math.min(start.x, x)); selection.setAttribute('width', verticalOnly ? width : Math.abs(x - start.x));
      selection.setAttribute('y', horizontalOnly ? T : Math.min(start.y,y)); selection.setAttribute('height', horizontalOnly ? height : Math.abs(start.y-y)); selection.setAttribute('visibility', 'visible');
      return;
    }
    const transform = svg.getScreenCTM(), scale = Math.hypot(transform.a,transform.b);
    inspect(snapChartX(xs,x,spec,6/scale), event.clientX, event.clientY);
  });
  svg.addEventListener('keydown', event => {
    if (['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key) && xs.length) {
      event.preventDefault();
      keyboardIndex = event.key === 'Home' ? 0 : event.key === 'End' ? xs.length - 1 : Math.max(0, Math.min(xs.length - 1, keyboardIndex + (event.key === 'ArrowRight' ? 1 : -1)));
      const x = sx(xs[keyboardIndex], spec);
      const anchor = new DOMPoint(x, T).matrixTransform(svg.getScreenCTM());
      inspect(x, anchor.x, anchor.y);
    } else if (event.key === '0') { event.preventDefault(); onZoom(null); }
    else if (event.key === 'Escape') { hideTooltip(); }
  });
  svg.addEventListener('blur', event => { if (!tooltip.contains(event.relatedTarget)) hideTooltip(); });
  svg.addEventListener('pointerleave', event => { if (!tooltip.contains(event.relatedTarget)) deferHide(); });
  tooltip.addEventListener('pointerleave', event => { if (!svg.contains(event.relatedTarget)) deferHide(); });
  tooltip.addEventListener('keydown', event => { if (event.key === 'Escape') hideTooltip(); });
  tooltip.addEventListener('focusout', event => { if (!tooltip.contains(event.relatedTarget) && event.relatedTarget !== svg) hideTooltip(); });
  const yValue = y => { const value = spec.yMax-(y-T)/height*(spec.yMax-spec.yMin); return spec.logScale ? 10 ** value : value; };
  svg.addEventListener('pointerdown', event => { if (event.button !== 0) return; start = plotPosition(event); svg.setPointerCapture(event.pointerId); hideTooltip(); });
  svg.addEventListener('pointerup', event => {
    if (start === null) return;
    const { x: end, y } = plotPosition(event), begin = start; start = null; selection.setAttribute('visibility', 'hidden');
    const horizontal = Math.abs(end - begin.x) > 8, vertical = Math.abs(y - begin.y) > 8;
    if (horizontal || vertical) {
      const range = horizontal ? [value(Math.min(begin.x,end)),value(Math.max(begin.x,end))] : [spec.xMin,spec.xMax];
      if (vertical) range.push(yValue(Math.max(y,begin.y)),yValue(Math.min(y,begin.y)));
      onZoom(range);
    }
  });
  svg.addEventListener('pointercancel', () => { start = null; selection.setAttribute('visibility', 'hidden'); });
  svg.addEventListener('dblclick', () => onZoom(null));
}
