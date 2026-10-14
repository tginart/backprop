/**
 * Architecture: explicit workspace/section overrides -> common form fields ->
 * validated JSON settings. Empty fields mean inherit, not a frozen default.
 * Main callers: workspace settings and section settings dialogs.
 * Why this exists: all scopes expose the same chart options and use the same
 * precedence in previews, rendering, exports and saved workspace recovery.
 */
import {escapeHtml as e} from './model.mjs';
import {validateEditorDraft} from './panel-editor.mjs';
import {resolvePanel} from './workspace.mjs';

const numbers=['smoothing','maxRuns','xMin','xMax','yMin','yMax'];
const booleans=['logScale','showRaw','showLegend'];
const text=['xAxis','smoothingMethod','kind','xLabel','yLabel'];
export function chartDefaultsFields(settings,axes,inherit='Inherit workspace') {
  const option=(key,label,choices)=>`<label>${label}<select name="${key}" aria-label="${label}"><option value="">${e(inherit)}</option>${choices.map(([value,name])=>`<option value="${e(value)}" ${String(settings[key])===String(value)?'selected':''}>${e(name)}</option>`).join('')}</select></label>`;
  const number=(key,label,extra='')=>`<label>${label}<input name="${key}" type="number" step="any" value="${e(settings[key]??'')}" placeholder="${e(inherit)}" ${extra}></label>`;
  return option('xAxis','Default X axis',axes)+`<div class="bound-grid">${number('xMin','X minimum')}${number('xMax','X maximum')}${number('yMin','Y minimum')}${number('yMax','Y maximum')}</div>`+
    option('logScale','Default Y scale',[[false,'Linear'],[true,'Logarithmic']])+option('kind','Plot style',[['line','Line'],['scatter','Scatter'],['area','Area'],['bar','Bar']])+
    option('smoothingMethod','Smoothing method',[['twema','Time-weighted EMA'],['ema','EMA']])+number('smoothing','Default smoothing','min="0" max="0.98"')+
    number('maxRuns','Max runs or groups','min="1" max="1000"')+option('showLegend','Run legend',[[true,'Show'],[false,'Hide']])+option('showRaw','Raw values',[[true,'Show'],[false,'Hide']])+
    ['xLabel','yLabel'].map(key=>`<label>${key==='xLabel'?'X':'Y'} axis label<input name="${key}" maxlength="160" value="${e(settings[key] || '')}" placeholder="Automatic"></label>`).join('');
}

export function readChartDefaults(form,inherited) {
  const values=new FormData(form),result={};
  for(const key of numbers)if(values.get(key)!=='' && values.has(key))result[key]=Number(values.get(key));
  for(const key of booleans)if(values.get(key)!=='' && values.has(key))result[key]=values.get(key)==='true';
  for(const key of text)if(values.get(key)!=='' && values.has(key))result[key]=values.get(key);
  const error=validateEditorDraft(resolvePanel(inherited,result));
  if(error)throw new Error(error);
  return result;
}
