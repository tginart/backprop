/**
 * Architecture: copy saved filters -> edit a local draft -> validate and preview
 * matching runs -> Apply publishes display preferences. Cancel has no effect.
 * Main caller: app's Filter action, using the existing inspection dialog.
 * Why this exists: filter editing must not change selected runs while a user is
 * typing an incomplete number or a field that is absent from this project.
 */
import {escapeHtml as e} from './model.mjs';
import {analysisFields, FILTER_OPERATORS, validateFilters, matchesFilters} from './run-analysis.mjs';

export function openRunFilters({dialog, runs, filters, onApply}) {
  let draft = filters.map(filter => ({...filter}));
  const fields = new Map(analysisFields(runs));
  for (const filter of draft) if (!fields.has(filter.field)) fields.set(filter.field, `${filter.field} (not logged here)`);
  const options = (items, selected) => [...items].map(([value, label]) => `<option value="${e(value)}" ${value === selected ? 'selected' : ''}>${e(label)}</option>`).join('');
  function render() {
    dialog.className = 'filter-dialog';
    dialog.innerHTML = `<header class="dialog-header"><h2>Filter runs</h2><button class="icon-btn" data-filter-action="cancel" aria-label="Close run filters">×</button></header><div class="dialog-body"><p class="dim">Match all conditions. Numbers, true, false and null use their JSON spelling; quote a numeric string.</p><div class="filter-rows">${draft.map((filter, index) => `<div class="filter-row" data-filter-row="${index}"><label>Field<select data-filter-part="field" aria-label="Filter field ${index+1}">${options(fields, filter.field)}</select></label><label>Comparison<select data-filter-part="op" aria-label="Filter comparison ${index+1}">${options(FILTER_OPERATORS, filter.op)}</select></label><label>Value<input data-filter-part="value" aria-label="Filter value ${index+1}" value="${e(filter.value ?? '')}" ${['exists','missing'].includes(filter.op) ? 'disabled' : ''}></label><button class="icon-btn" data-filter-remove="${index}" aria-label="Remove filter ${index+1}">×</button></div>`).join('') || '<p class="dim">No conditions. All runs match.</p>'}</div><button class="btn small" data-filter-action="add">+ Add condition</button><p data-filter-preview role="status"></p></div><footer class="dialog-footer"><button class="btn" data-filter-action="clear">Clear conditions</button><span class="grow"></span><button class="btn" data-filter-action="cancel">Cancel</button><button class="btn primary" data-filter-action="apply">Apply filters</button></footer>`;
    updatePreview();
  }
  function updatePreview() {
    const preview = dialog.querySelector('[data-filter-preview]'), apply = dialog.querySelector('[data-filter-action="apply"]');
    try { validateFilters(draft); preview.textContent = `${runs.filter(run => matchesFilters(run, draft)).length} of ${runs.length} runs match`; preview.className = 'dim'; apply.disabled = false; }
    catch (error) { preview.textContent = error.message; preview.className = 'filter-error'; apply.disabled = true; }
  }
  const controller = new AbortController(), optionsWithSignal = {signal:controller.signal};
  dialog.addEventListener('input', event => {
    const part = event.target.dataset.filterPart; if (!part) return;
    const index = Number(event.target.closest('[data-filter-row]').dataset.filterRow);
    draft[index][part] = event.target.value;
    if (part === 'op') { dialog.querySelector(`[data-filter-row="${index}"] [data-filter-part="value"]`).disabled = ['exists','missing'].includes(event.target.value); }
    updatePreview();
  }, optionsWithSignal);
  dialog.addEventListener('click', event => {
    const target = event.target.closest('button'); if (!target) return;
    if (target.dataset.filterRemove !== undefined) { draft.splice(Number(target.dataset.filterRemove), 1); render(); return; }
    switch (target.dataset.filterAction) {
      case 'add': draft.push({field:'run:state',op:'eq',value:'finished'}); render(); break;
      case 'clear': draft = []; render(); break;
      case 'cancel': dialog.close(); break;
      case 'apply': validateFilters(draft); onApply(draft.map(filter => ({...filter}))); dialog.close(); break;
    }
  }, optionsWithSignal);
  dialog.addEventListener('close', () => controller.abort(), {once:true});
  render();
  dialog.showModal();
}
