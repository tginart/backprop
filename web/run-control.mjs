/**
 * Architecture: explicit stop action -> fresh instance check -> optional OAuth
 * consent -> confirmed stream write -> SDK acknowledgment and finish polling.
 * Main callers: app run details. Adapters own auth/Foundry requests and model
 * updates; this module owns the dialog and never sends a request on sign-in.
 * Invariants: one request ID survives retries, resumed processes require a new
 * confirmation, accepted means callback delivery rather than process completion.
 */
import {escapeHtml as e, normalizeEvents} from './model.mjs';

export function stopPhase(run, instanceId, attempt = null) {
  if (!run) return {kind:'unavailable', message:'The run could not be found.'};
  if (!run.control?.stop_supported || !instanceId) return {kind:'unavailable', message:'This process does not advertise remote stop support.'};
  if (run.control?.instance_id !== instanceId) return {kind:'changed', message:'This run has restarted or no longer accepts stop requests. Close this dialog and inspect its current process.'};
  if (run.state !== 'running') return {kind:'finished', message:`The run is ${run.state}. No stop request is needed.`};
  const requests = run.stop_requests || [];
  const ack = requests.find(request => request.status === 'failed') || requests.find(request => request.status === 'accepted');
  if (ack?.status === 'failed') return {kind:'failed', message:`The SDK accepted the request, but its stop callback failed: ${ack.error || 'unknown error'}. The run has not reported completion.`, request:ack};
  if (ack) return {kind:'accepted', message:'The SDK accepted the stop request. Waiting for its stop callback and for the run to report completion.', request:ack};
  const pending = requests.find(request => request.request_id === attempt?.requestId) || requests[0];
  if (pending || attempt?.sent) return {kind:'requested', message:'The request was sent. Waiting for the running SDK to acknowledge it.', request:pending};
  if (attempt) return {kind:'uncertain', message:'Delivery could not be confirmed. The request may have arrived. Checking status or retrying uses the same request ID.'};
  return {kind:'confirm', message:'Ask this running process to stop. Its SDK invokes the configured stop callback, or interrupts the training process when no callback was supplied.'};
}

export function createRunControl({dialog, read, publish, auth, consent, onUpdate = () => {}}) {
  let current = null, generation = 0, timer = null, controller = null;
  const close = () => { generation++; clearTimeout(timer); controller?.abort(); controller = null; current = null; };
  dialog.addEventListener('close', close);
  document.addEventListener('visibilitychange', () => {
    if (current && document.visibilityState === 'visible') refresh();
  });

  function render() {
    if (!current || !dialog.open) return;
    const focus = dialog.contains(document.activeElement) ? document.activeElement.dataset.controlAction : null;
    const {run, phase, busy, error, checkedAt, attempt} = current;
    const canWrite = auth().canControlRuns, confirm = phase.kind === 'confirm', uncertain = phase.kind === 'uncertain';
    const descriptions = {checking:'Checking the latest run',confirm:'Stop this run?',requested:'Stop requested',uncertain:'Check stop delivery',accepted:'SDK accepted the request',failed:'Stop callback failed',finished:'Run completed',changed:'Run process changed',unavailable:'Run unavailable'};
    dialog.innerHTML = `<header class="dialog-header"><div><div class="eyebrow">Run control</div><h2 id="run-control-title">${e(descriptions[phase.kind])}</h2></div><button class="icon-btn" data-control-action="close" aria-label="Close stop request">×</button></header><div class="dialog-body"><div class="control-run"><strong>${e(run.name)}</strong><span class="mono">${e([run.entity,run.project,run.id].filter(Boolean).join('/'))}</span></div><p class="control-status" role="status">${e(phase.message)}</p>${error ? `<div class="notice error" role="alert">${e(error)}</div>` : ''}${confirm && !canWrite ? '<div class="notice">Enable stop controls to grant Foundry stream-write permission. Your account also needs write access to the event stream. After sign-in, you must confirm the stop request separately.</div>' : ''}<p class="table-note">Targets this process only. A resumed process requires a new request.</p>${checkedAt ? `<p class="table-note">Status checked ${e(new Date(checkedAt).toLocaleTimeString())}${['requested','accepted','uncertain'].includes(phase.kind) ? ' · checks every 15 seconds while this dialog is open' : ''}.</p>` : ''}${attempt?.requestId || phase.request?.request_id ? `<details class="control-request"><summary>Request details</summary><code>${e(attempt?.requestId || phase.request.request_id)}</code></details>` : ''}</div><footer class="dialog-footer"><button class="btn" data-control-action="close">${confirm ? 'Cancel' : 'Close'}</button><span class="grow"></span>${phase.kind !== 'finished' ? `<button class="btn" data-control-action="refresh" ${busy ? 'disabled' : ''}>${busy ? 'Checking...' : 'Check status'}</button>` : ''}${confirm ? `<button class="btn ${canWrite ? 'danger' : 'primary'}" data-control-action="${canWrite ? 'send' : 'consent'}" ${busy ? 'disabled' : ''}>${canWrite ? 'Send stop request' : 'Enable stop controls'}</button>` : uncertain && canWrite ? `<button class="btn danger" data-control-action="send" ${busy ? 'disabled' : ''}>Retry same request</button>` : ''}</footer>`;
    if (focus) dialog.querySelector(`[data-control-action="${focus}"]`)?.focus({preventScroll:true});
  }

  function schedule() {
    clearTimeout(timer);
    if (current && ['requested','accepted','uncertain'].includes(current.phase.kind)) timer = setTimeout(() => refresh({refreshArchive:false}), 15000);
  }

  async function check(token, refreshArchive = false) {
    const snapshot = await read(current.run, {signal:controller.signal, refreshArchive});
    if (token !== generation || !current || !dialog.open) return false;
    const run = normalizeEvents(snapshot.events).runs.find(run => run.key === current.run.key);
    if (run) { current.run = run; onUpdate(run); }
    current.phase = stopPhase(run, current.instanceId, current.attempt);
    current.checkedAt = snapshot.fetchedAt;
    return true;
  }

  async function refresh({refreshArchive = true} = {}) {
    if (!current || current.busy) return;
    const token = generation;
    current.busy = true; current.error = ''; render();
    try { await check(token, refreshArchive); }
    catch (error) { if (token === generation && current) current.error = error.message || 'Could not read run status.'; }
    finally { if (token === generation && current) { current.busy = false; render(); schedule(); } }
  }

  async function send() {
    if (!current || current.busy || !['confirm','uncertain'].includes(current.phase.kind)) return;
    const token = generation;
    current.busy = true; current.error = ''; render();
    try {
      // Re-read immediately before publishing. We retain the originally shown
      // instance even if a resume appears during the request, never retarget it.
      if (!await check(token, true) || !['confirm','uncertain'].includes(current.phase.kind)) return;
      if (!auth().canControlRuns) throw new Error('Enable stop controls before sending a request.');
      current.attempt ||= {requestId:crypto.randomUUID(), timestamp:new Date().toISOString(), sent:false};
      const attempt = current.attempt, run = current.run;
      await publish({id:run.id, project:run.project, entity:run.entity, instanceId:current.instanceId,
        requestId:attempt.requestId, timestamp:attempt.timestamp}, {signal:controller.signal});
      if (token !== generation || !current) return;
      attempt.sent = true;
      current.phase = stopPhase(current.run, current.instanceId, attempt);
    } catch (error) {
      if (token === generation && current) {
        current.error = error.message || 'The stop request could not be confirmed.';
        // An explicit authorization/validation refusal did not publish a stop.
        // Network failures retain identity because delivery may have succeeded.
        if ([400,401,403,404,422].includes(error.status)) current.attempt = null;
        current.phase = stopPhase(current.run, current.instanceId, current.attempt);
      }
    } finally { if (token === generation && current) { current.busy = false; render(); schedule(); } }
  }

  dialog.addEventListener('click', async event => {
    const action = event.target.closest('[data-control-action]')?.dataset.controlAction;
    if (action === 'close') dialog.close();
    else if (action === 'refresh') await refresh();
    else if (action === 'send') await send();
    else if (action === 'consent' && current && !current.busy) {
      current.busy = true; render();
      const run = current.run, returnUrl = new URL(location.href);
      returnUrl.searchParams.delete('entity'); returnUrl.searchParams.set('project',`${run.entity}/${run.project}`);
      returnUrl.searchParams.set('run',run.id); returnUrl.searchParams.set('control','stop');
      try { await consent({allowRunControl:true,returnUrl:returnUrl.href}); }
      catch(error) { if(current) { current.error=error.message; current.busy=false; render(); } }
    }
  });

  return {open(run) {
    close(); controller = new AbortController();
    current = {run, instanceId:run.control?.instance_id, attempt:null, busy:false, error:'',
      phase:{kind:'checking',message:'Reading the latest run state before enabling any action.'}};
    dialog.className='small-dialog run-control-dialog'; dialog.setAttribute('aria-labelledby','run-control-title');
    if (!dialog.open) dialog.showModal(); render(); refresh();
  }};
}
