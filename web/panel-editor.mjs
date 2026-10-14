/**
 * Architecture: saved settings -> isolated form draft -> Data/Display controls
 * and live preview -> validation -> Apply. Reset changes only the draft;
 * Cancel, Escape and closing never change saved workspace or panel state.
 * Main caller: app.mjs openPanel(). The workspace supplies preview rendering so
 * previews and live charts share source selection, run limits and chart options.
 * Invariant: only values different from inherited defaults are committed.
 */
import { escapeHtml as e } from './model.mjs';
import { resolvePanel, validatePanel } from './workspace.mjs';

/** Resolve editor values while preserving live workspace/section inheritance.
 * Main callers: opening the editor, draft reset and override comparison.
 * Why this exists: empty labels and the default run limit need concrete form
 * values without freezing those values into every saved panel's overrides.
 */
export function resolveEditorDraft(defaults, settings = {}, metric) {
  const inherited = {secondMetric:'', yMetric:metric, ...resolvePanel(defaults),
    maxRuns:defaults.maxRuns ?? 10, xLabel:defaults.xLabel ?? '', yLabel:defaults.yLabel ?? '', groupBy:'inherit', aggregation:'inherit',range:'inherit'};
  for (const key of ['showLegend', 'showRaw']) if (defaults[key] !== undefined) inherited[key] = defaults[key];
  return {...inherited, ...settings};
}

/** Validate the whole draft before previewing or applying it.
 * Main callers: live preview and Apply. The run limit counts visible runs in
 * workspace order; it must be a whole number between 1 and 1000.
 */
export function validateEditorDraft(draft) {
  const panelError = validatePanel(draft);
  if (panelError) return panelError;
  const maxRuns = draft.maxRuns === undefined ? 10 : draft.maxRuns;
  if (!Number.isInteger(maxRuns) || maxRuns < 1 || maxRuns > 1000) return 'Max runs must be a whole number between 1 and 1000.';
  for (const key of ['xLabel', 'yLabel']) if (draft[key] !== undefined && typeof draft[key] !== 'string') return 'Axis labels must be text.';
  return '';
}

/** Keep only explicit differences, allowing future default changes to flow in.
 * Main callers: Apply. An empty result removes all saved panel overrides.
 */
export function editorOverrides(draft, defaults, metric) {
  const inherited = resolveEditorDraft(defaults, {}, metric);
  return Object.fromEntries(Object.entries(draft).filter(([key, value]) => value !== inherited[key]));
}

/** Open one transactional edit session with a live preview and keyboard tabs.
 * Main callers: existing-panel editing and the Add panels scalar creation flow.
 * Reset and tab switching remain inside this session; only Apply calls onApply.
 */
export function openPanelEditor({ dialog, metric, metrics, defaults, settings, groupFields=[], renderPreview, onApply }) {
  const draft = resolveEditorDraft(defaults, settings, metric);
  const axes = [['defined', 'Defined step'], ['_step', 'Step'], ['_runtime', 'Relative time (s)'], ['_timestamp', 'Wall time'], ...metrics.map(key => [key, key])];
  const options = (entries, value) => entries.map(([key, label]) => `<option value="${e(key)}" ${key === value ? 'selected' : ''}>${e(label)}</option>`).join('');
  const bound = (axis, end) => `<label>${axis.toUpperCase()} ${end === 'Min' ? 'minimum' : 'maximum'}<input type="number" step="any" name="${axis}${end}" value="${e(draft[axis + end] ?? '')}" placeholder="Auto"></label>`;
  dialog.className = 'panel-editor';
  dialog.innerHTML = `<form id="panel-form">
    <header class="dialog-header"><div><div class="eyebrow">Panel settings</div><h2>${e(metric)}</h2></div><button type="button" class="icon-btn" data-editor-close aria-label="Cancel panel editing">✕</button></header>
    <div class="editor-layout">
      <div class="editor-preview"><div class="preview-label">Preview <span>${e(metric)}</span></div><div data-panel-preview></div><p class="table-note" data-preview-limit></p><p class="table-note">Changes apply to this panel only. Reset changes the draft; Apply saves it.</p></div>
      <div class="editor-controls">
        <label>Panel title<input name="title" value="${e(draft.title)}" placeholder="${e(metric)}" maxlength="160"></label>
        <nav class="dialog-tabs editor-tabs" role="tablist" aria-label="Panel settings categories"><button type="button" class="dialog-tab active" role="tab" id="panel-editor-data-tab" data-editor-tab="data" aria-controls="panel-editor-data" aria-selected="true">Data</button><button type="button" class="dialog-tab" role="tab" id="panel-editor-display-tab" data-editor-tab="display" aria-controls="panel-editor-display" aria-selected="false" tabindex="-1">Display</button></nav>
        <fieldset id="panel-editor-data" role="tabpanel" aria-labelledby="panel-editor-data-tab" data-editor-section="data"><legend>Data</legend>
          <label>Y metric<select name="yMetric" aria-label="Y metric">${options(metrics.map(key => [key, key]), draft.yMetric)}</select></label>
          <label>Compare a second metric<select name="secondMetric" aria-label="Compare a second metric"><option value="">None</option>${options(metrics.map(key => [key, key]), draft.secondMetric)}</select></label>
          <label>Group runs<select name="groupBy" aria-label="Panel grouping">${options([['inherit','Inherit workspace grouping'],['','No grouping'],['all','All visible runs'],...groupFields],draft.groupBy)}</select></label>
          <label>Aggregation<select name="aggregation">${options([['inherit','Inherit workspace'],['mean','Mean'],['median','Median'],['min','Minimum'],['max','Maximum']],draft.aggregation)}</select></label>
          <label>Variation band<select name="range">${options([['inherit','Inherit workspace'],['stddev','Standard deviation'],['stderr','Standard error'],['minmax','Min / max'],['none','None']],draft.range)}</select></label>
          <label>X axis<select name="xAxis" aria-label="X axis">${options(axes, draft.xAxis)}</select></label>
          <div class="bound-grid">${bound('x', 'Min')}${bound('x', 'Max')}${bound('y', 'Min')}${bound('y', 'Max')}</div>
          <label class="check-label"><input type="checkbox" name="logScale" ${draft.logScale ? 'checked' : ''}> Logarithmic Y axis</label>
        </fieldset>
        <fieldset id="panel-editor-display" role="tabpanel" aria-labelledby="panel-editor-display-tab" data-editor-section="display" hidden style="display:none"><legend>Display</legend>
          <label>Plot style<select name="kind" aria-label="Plot style">${options([['line','Line'],['scatter','Scatter'],['area','Area'],['bar','Bar']], draft.kind)}</select></label>
          <label>Max runs<input type="number" name="maxRuns" min="1" max="1000" step="1" required value="${e(draft.maxRuns)}"></label>
          <p class="table-note">Uses the first visible runs in workspace order.</p>
          <label>X axis label<input name="xLabel" value="${e(draft.xLabel)}" placeholder="Automatic" maxlength="160"></label>
          <label>Y axis label<input name="yLabel" value="${e(draft.yLabel)}" placeholder="Automatic" maxlength="160"></label>
          <label>Smoothing method<select name="smoothingMethod" aria-label="Smoothing method">${options([['twema','Time-weighted EMA'],['ema','EMA']], draft.smoothingMethod)}</select></label>
          <label>Smoothing <span class="editor-smoothing">${draft.smoothing.toFixed(2)}</span><input type="range" name="smoothing" min="0" max="0.98" step="0.02" value="${draft.smoothing}"></label>
          <label class="check-label"><input type="checkbox" name="showRaw" ${draft.showRaw ? 'checked' : ''}> Show raw values behind smoothed line</label>
          <label class="check-label"><input type="checkbox" name="showLegend" ${draft.showLegend ? 'checked' : ''}> Show run legend and latest values</label>
        </fieldset>
      </div>
    </div>
    <div class="editor-error notice error" role="alert" hidden></div>
    <footer class="dialog-footer"><button type="button" class="btn" data-editor-reset>Use inherited defaults</button><span class="grow"></span><button type="button" class="btn" data-editor-close>Cancel</button><button type="submit" class="btn primary">Apply settings</button></footer>
  </form>`;
  const form = dialog.querySelector('form');
  const read = () => {
    const values = new FormData(form), result = {title:values.get('title'), yMetric:values.get('yMetric'), secondMetric:values.get('secondMetric'),
      xAxis:values.get('xAxis'), kind:values.get('kind'), smoothing:Number(values.get('smoothing')), smoothingMethod:values.get('smoothingMethod'),
      groupBy:values.get('groupBy'),aggregation:values.get('aggregation'),range:values.get('range'), maxRuns:Number(values.get('maxRuns')), xLabel:values.get('xLabel'), yLabel:values.get('yLabel')};
    for (const key of ['logScale', 'showRaw', 'showLegend']) result[key] = values.has(key);
    for (const key of ['xMin', 'xMax', 'yMin', 'yMax']) if (values.get(key) !== '') result[key] = Number(values.get(key));
    return result;
  };
  const updatePreview = () => {
    const value = read(), error = validateEditorDraft(value), notice = dialog.querySelector('.editor-error');
    notice.textContent = error; notice.hidden = !error;
    form.querySelector('[type="submit"]').disabled = Boolean(error);
    dialog.querySelector('.editor-smoothing').textContent = value.smoothing.toFixed(2);
    dialog.querySelector('[data-preview-limit]').textContent = error ? '' : `Preview run limit: ${value.maxRuns}. Uses the first visible runs in workspace order.`;
    if (!error) renderPreview(dialog.querySelector('[data-panel-preview]'), value);
  };
  const tabs = [...form.querySelectorAll('[data-editor-tab]')];
  const selectTab = selected => {
    for (const tab of tabs) {
      const active = tab === selected;
      tab.setAttribute('aria-selected', String(active)); tab.tabIndex = active ? 0 : -1; tab.classList.toggle('active', active);
      const panel = form.querySelector(`[data-editor-section="${tab.dataset.editorTab}"]`);
      panel.hidden = !active; panel.style.display = active ? '' : 'none';
    }
  };
  for (const tab of tabs) {
    tab.addEventListener('click', () => selectTab(tab));
    tab.addEventListener('keydown', event => {
      if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
      event.preventDefault();
      const index = event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1
        : (tabs.indexOf(tab) + (event.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length;
      selectTab(tabs[index]); tabs[index].focus();
    });
  }
  form.addEventListener('input', updatePreview);
  form.addEventListener('change', updatePreview);
  form.addEventListener('submit', event => {
    event.preventDefault(); const value = read(); if (validateEditorDraft(value)) return;
    onApply(editorOverrides(value, defaults, metric)); dialog.close();
  });
  for (const button of form.querySelectorAll('[data-editor-close]')) button.addEventListener('click', () => dialog.close());
  form.querySelector('[data-editor-reset]').addEventListener('click', () => {
    const inherited = resolveEditorDraft(defaults, {}, metric);
    for (const control of form.querySelectorAll('[name]')) {
      if (control.type === 'checkbox') control.checked = Boolean(inherited[control.name]);
      else control.value = inherited[control.name] ?? '';
    }
    updatePreview();
  });
  if (!dialog.open) dialog.showModal();
  updatePreview();
}
