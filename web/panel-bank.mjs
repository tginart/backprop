import {CUSTOM_WORKSPACES} from './experience.mjs';
/**
 * Architecture: typed panel definition -> visible run/writer snapshots -> shared
 * rich renderer; catalog -> isolated creation/edit dialog -> explicit Apply.
 * Main callers: app.mjs panel bank and panel actions. This module owns panel UI,
 * not transport or event replay. Missing values and display limits stay visible.
 * Why this exists: tables/media need the same placement, source selection and
 * saved display identity as line charts instead of an unrelated data route.
 */
import {escapeHtml as e} from './model.mjs';
import {createPanel,panelVersions,resolvePanelData} from './panel-catalog.mjs';

/** Render rich snapshots with independent writer/step identity.
 * Main callers: project and single-run panel banks. Each visible record uses the
 * existing renderer, so artifacts, annotations and table drilldowns keep working.
 */
export function renderLoggedPanel({id,definition,runs,settings={},placement={},columns,expanded,icon,renderValue,steps,register}) {
  const source={...definition,source:settings.source || definition.source};
  const limit=settings.maxRuns || 10;
  const entries=runs.flatMap(run=>{
    const versions=panelVersions(source,run),writers=[...new Set(versions.map(version=>version.writer))];
    return (writers.length?writers:[undefined]).map(writer=>{
      const key=JSON.stringify([id,run.key,writer || null]),chosen=steps.get(key);
      return {run,writer,key,versions:versions.filter(version=>version.writer===writer),chosen,
        ...resolvePanelData(source,run,{step:chosen,writer})};
    });
  });
  const available=entries.filter(entry=>!entry.missing),displayed=entries.filter(entry=>!entry.missing || entry.chosen!==undefined&&entry.versions.length).slice(0,limit);
  const content=displayed.map(entry=>{
    if(!entry.missing)register(entry.key,entry.value);
    const label=`${entry.run.name}${entry.writer?' / '+entry.writer:''}`;
    return `<div class="logged-panel-entry"><div class="rich-history"><span class="dot" style="background:${entry.run.color}"></span><button class="text-button" data-run="${e(entry.run.key)}">${e(label)}</button>${entry.location==='summary'?'<span class="tag">Summary</span>':`<label>Step <select data-rich-step="${e(entry.key)}" aria-label="History step for ${e(definition.source)} in ${e(label)}"><option value="">Latest</option>${entry.versions.slice().reverse().map(version=>`<option value="${e(version.step)}" ${String(version.step)===entry.chosen?'selected':''}>${e(version.step)}</option>`).join('')}</select></label>`}<button class="icon-btn" data-export-rich="${e(entry.key)}" aria-label="Download ${e(label)} logged value">${icon('download')}</button></div>${entry.missing?'<p class="chart-empty">No value at this step. Select another recorded step or Latest.</p>':renderValue(entry.value,entry.key,entry.run.color)}</div>`;
  }).join('');
  const title=settings.title || definition.title;
  return `<article class="chart-card arranged-panel logged-panel" data-metric="${e(id)}" data-source="${e(source.source)}" data-panel-type="${e(definition.type)}" style="--panel-span:${Math.min(columns,placement.span || 1)};--panel-height:${placement.height || 400}px"><div class="chart-header">${CUSTOM_WORKSPACES?`<button class="panel-drag" draggable="true" data-panel-drag="${e(id)}" aria-label="Drag ${e(title)} panel">⠿</button>`:''}<h3 class="chart-title" title="${e(title)}">${e(title)}</h3><div class="chart-actions">${CUSTOM_WORKSPACES?`<button class="icon-btn" data-edit-panel="${e(id)}" aria-label="Edit ${e(title)} panel">${icon('settings')}</button>`:''}${expanded?'':`<button class="icon-btn" data-expand="${e(id)}" aria-label="Expand ${e(title)}">${icon('expand')}</button>`}<button class="icon-btn" data-panel-menu="${e(id)}" aria-label="Actions for ${e(title)} panel" aria-haspopup="menu">⋯</button></div></div><div class="chart-subtitle">${e(definition.type)} · ${available.length>limit?`Showing ${limit} of ${available.length}`:available.length} logged ${available.length===1?'output':'outputs'}${entries.length>available.length?` · ${entries.length-available.length} without data`:''}</div><div class="logged-panel-body">${content || '<div class="chart-empty">No compatible logged values in the visible runs.</div>'}</div>${expanded?'':`<div class="chart-footer"><span>${e(source.source)}</span>${CUSTOM_WORKSPACES?`<button class="panel-resizer" data-panel-resize="${e(id)}" aria-label="Resize ${e(title)} panel">◢</button>`:''}</div>`}</article>`;
}

/** Quick Add creates a new independent panel from a real recorded source.
 * Main callers: Add panels on the panel bank and section menu. Closing the
 * library changes nothing; Add commits only the chosen definition.
 */
export function openPanelLibrary({dialog,sources,section,onAdd}) {
  dialog.className='panel-library';
  dialog.innerHTML=`<header class="dialog-header"><div><h2>Add panels</h2><p class="dim">Quick add from logged keys</p></div><button class="icon-btn" data-library-close aria-label="Close panel library">✕</button></header><div class="dialog-body"><div class="panel-library-special"><h3>Evaluation</h3><button class="btn" data-add-comparer>Run comparer</button></div><h3>Logged keys</h3><input type="search" data-source-search aria-label="Search available panel sources" placeholder="Search logged keys"><div class="panel-source-list">${sources.map((source,index)=>`<div class="panel-source" data-source-index="${index}"><div><strong>${e(source.title)}</strong><span class="dim">${e(source.type)} · ${source.runKeys.length} runs</span></div><button class="btn small" data-add-source="${index}" aria-label="Add ${e(source.type)} panel for ${e(source.source)}">Add</button></div>`).join('')}</div><p data-no-sources hidden>No matching logged keys.</p><p data-library-error role="alert" hidden></p></div>`;
  dialog.querySelector('[data-add-comparer]').addEventListener('click',()=>onAdd(createPanel({type:'run-comparer',source:'__runs__',title:'Run comparer',section:section || 'Comparison'})));
  dialog.querySelector('[data-library-close]').addEventListener('click',()=>dialog.close());
  dialog.querySelector('[data-source-search]').addEventListener('input',event=>{
    let count=0;for(const row of dialog.querySelectorAll('[data-source-index]')){const source=sources[Number(row.dataset.sourceIndex)];row.hidden=!`${source.source} ${source.type}`.toLowerCase().includes(event.target.value.toLowerCase());if(!row.hidden)count++;}
    dialog.querySelector('[data-no-sources]').hidden=count>0;
  });
  for(const button of dialog.querySelectorAll('[data-add-source]'))button.addEventListener('click',()=>{
    try{const source=sources[Number(button.dataset.addSource)];onAdd(createPanel({...source,...(section?{section}: {})}));button.textContent='Added';button.setAttribute('aria-label',`Add another ${source.type} panel for ${source.source}`);}
    catch(error){const node=dialog.querySelector('[data-library-error]');node.hidden=false;node.textContent=error.message;}
  });
  if(!dialog.open)dialog.showModal();dialog.querySelector('[data-source-search]').focus();
}

/** Edit rich panel display/source settings as one cancellable draft.
 * Main callers: panel Edit. The source type cannot silently change underneath an
 * existing viewer, and changing title does not rename a logged key.
 */
export function openLoggedPanelEditor({dialog,definition,settings={},sources,onApply}) {
  const compatible=sources.filter(source=>source.type===definition.type);
  if(!compatible.some(source=>source.source===(settings.source || definition.source)))compatible.unshift({source:settings.source || definition.source});
  dialog.className='small-dialog';
  dialog.innerHTML=`<form><header class="dialog-header"><h2>Edit ${e(definition.type)} panel</h2><button type="button" class="icon-btn" data-editor-cancel aria-label="Cancel panel editing">✕</button></header><div class="dialog-body analysis-settings"><label>Panel title<input name="title" required maxlength="160" value="${e(settings.title || definition.title)}"></label><label>Logged key<select name="source">${compatible.map(item=>`<option value="${e(item.source)}" ${item.source===(settings.source || definition.source)?'selected':''}>${e(item.source)}</option>`).join('')}</select></label><label>Maximum runs or writers<input name="maxRuns" type="number" min="1" max="1000" required value="${settings.maxRuns || 10}"></label><p class="dim">Step selection is available within each logged output.</p><p data-editor-error role="alert" hidden></p></div><footer class="dialog-footer"><button type="button" class="btn" data-editor-cancel>Cancel</button><button class="btn primary">Apply settings</button></footer></form>`;
  for(const button of dialog.querySelectorAll('[data-editor-cancel]'))button.addEventListener('click',()=>dialog.close());
  dialog.querySelector('form').addEventListener('submit',event=>{
    event.preventDefault();const values=new FormData(event.currentTarget),maxRuns=Number(values.get('maxRuns')),title=String(values.get('title')).trim();
    if(!title || !Number.isInteger(maxRuns) || maxRuns<1 || maxRuns>1000){const error=dialog.querySelector('[data-editor-error]');error.hidden=false;error.textContent='Enter a title and a run limit from 1 to 1000.';return;}
    onApply({...settings,title,source:values.get('source'),maxRuns});dialog.close();
  });
  if(!dialog.open)dialog.showModal();
}
