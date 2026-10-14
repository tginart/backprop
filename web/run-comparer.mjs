/**
 * Architecture: first visible, filtered runs -> recorded metadata/config/summary
 * -> structural difference rows -> a normal workspace panel.
 * Main callers: panel bank and expanded panel. Row checkboxes never choose these
 * columns; the same eye/filter/sort controls that feed charts choose them.
 * Missing, null, zero and rich values remain distinguishable.
 */
import {escapeHtml as e,flatten,isRich} from './model.mjs';
import {comparisonRows} from './workspace.mjs';

export function renderRunComparer({id,title,runs,settings={},placement={},columns,expanded,icon,valueCell}) {
  const shown=runs.slice(0,settings.maxRuns || 10),search=settings.search || '',differences=!!settings.differences;
  const sections=[['Run',shown.map(run=>new Map(Object.entries({name:run.name,id:run.id,state:run.state,group:run.group,job_type:run.job_type,notes:run.notes,created_at:run.created_at}).filter(([,value])=>value!==undefined)))],
    ['Config',shown.map(run=>new Map(flatten(run.config)))],['Summary',shown.map(run=>new Map(flatten(run.summary).filter(([,value])=>!isRich(value)||value._type==='number')))]];
  const rows=sections.map(([section,values])=>{
    const matches=comparisonRows(values,{search,differences});
    return matches.length?`<tr class="comparison-section"><th colspan="${shown.length+1}">${e(section)}</th></tr>${matches.map(({key,values,present,different})=>`<tr><th scope="row">${e(key)}</th>${values.map((value,index)=>`<td class="${different?'diff-cell':''}">${present[index]?`<div class="comparer-cell"><span>${value===null?'null':valueCell(value)}</span><button class="icon-btn" data-copy="${e(typeof value==='string'?value:JSON.stringify(value))}" aria-label="Copy ${e(key)} from ${e(shown[index].name)}">${icon('copy')}</button></div>`:'<span class="dim">Not logged</span>'}</td>`).join('')}</tr>`).join('')}`:'';
  }).join('');
  return `<article class="chart-card arranged-panel run-comparer" data-metric="${e(id)}" data-panel-type="run-comparer" style="--panel-span:${Math.min(columns,placement.span || columns)};--panel-height:${placement.height || 470}px"><div class="chart-header"><button class="panel-drag" draggable="true" data-panel-drag="${e(id)}" aria-label="Drag ${e(title)} panel">⠿</button><h3 class="chart-title">${e(title)}</h3><div class="chart-actions"><button class="icon-btn" data-edit-panel="${e(id)}" aria-label="Edit ${e(title)} panel">${icon('settings')}</button>${expanded?'':`<button class="icon-btn" data-expand="${e(id)}" aria-label="Expand ${e(title)}">${icon('expand')}</button>`}<button class="icon-btn" data-panel-menu="${e(id)}" aria-label="Actions for ${e(title)} panel" aria-haspopup="menu">⋯</button></div></div><div class="comparer-toolbar"><input type="search" data-comparer-option="search" data-comparer="${e(id)}" value="${e(search)}" placeholder="Search fields" aria-label="Search comparison fields"><label><input type="checkbox" data-comparer-option="differences" data-comparer="${e(id)}" ${differences?'checked':''}> Diff only</label><span class="dim">First ${shown.length} of ${runs.length} visible runs</span></div><div class="table-wrap comparison-table" tabindex="0" aria-label="Run comparison"><table><thead><tr><th>Field</th>${shown.map(run=>`<th><button class="text-button" data-run="${e(run.key)}"><span class="dot" style="background:${run.color}"></span>${e(run.name)}</button></th>`).join('')}</tr></thead><tbody>${rows || `<tr><td colspan="${shown.length+1}">${shown.length?'No matching fields.':'Show runs to compare their recorded values.'}</td></tr>`}</tbody></table></div>${expanded?'':`<div class="chart-footer"><span>Config and summary</span><button class="panel-resizer" data-panel-resize="${e(id)}" aria-label="Resize ${e(title)} panel">◢</button></div>`}</article>`;
}
