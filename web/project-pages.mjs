/**
 * Architecture: replayed project/run records -> collection/version index ->
 * selected artifact tab -> existing run/file/table inspection actions.
 * Main callers: app.mjs project navigation and artifact selection handlers.
 * Product rules: identity includes immutable ID and recorded version. Repeated
 * logging contributes run relationships, never duplicate versions. Missing IDs
 * stay inspectable as separate snapshots with a visible warning. Usage is
 * source-scoped; an omitted version cannot identify one of several versions.
 * Why this exists: project browsing owns different selection state from the run
 * inspector, while both views use the same file and table actions.
 */
import {escapeHtml as e, metricKeys} from './model.mjs';

export const ARTIFACT_TABS = Object.freeze(['version', 'metadata', 'usage', 'files', 'lineage']);
const present = value => value !== undefined && value !== null && value !== '';
const versionName = artifact => present(artifact.version) ? (typeof artifact.version === 'number' ? `v${artifact.version}` : String(artifact.version)) : null;
const runIdentity = run => run.key || JSON.stringify([run.entity || '', run.project, run.id]);
const uniqueRuns = runs => [...new Map(runs.map(run => [runIdentity(run), run])).values()];
const recordedId = artifact => present(artifact.artifact_id) ? artifact.artifact_id : present(artifact.id) ? artifact.id : null;
const trustedMarkup = Symbol('renderer markup');
const markup = html => ({[trustedMarkup]:html});
const collectionName = artifact => {
  if (present(artifact.collection_name)) return artifact.collection_name;
  const name = artifact.name || 'Unnamed artifact collection', version = versionName(artifact);
  return version && name.endsWith(`:${version}`) ? name.slice(0, -version.length - 1) : name;
};
const fields = entries => `<div class="detail-overview">${entries.map(([label, value]) => `<div><label>${e(label)}</label>${value?.[trustedMarkup] !== undefined ? `<div style="margin-top:7px">${value[trustedMarkup]}</div>` : `<p>${e(present(value) ? value : 'Not recorded')}</p>`}</div>`).join('')}</div>`;
const tags = values => values?.length ? values.map(value => `<span class="tag" title="${e(value === '' ? 'Empty value' : value)}">${e(value === '' ? '""' : value)}</span>`).join(' ') : '<span class="dim">None recorded</span>';
const recordedDate = (value, date) => present(value) ? date(value) : 'Not recorded';
const qualifiedName = (collection, version) => version.artifact.qualified_name || [...[version.runs[0].entity, version.runs[0].project].filter(Boolean), collection.name + (versionName(version.artifact) ? `:${versionName(version.artifact)}` : '')].join('/');
const formatBytes = bytes => {
  if (bytes < 1024) return `${bytes} B`;
  const power = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), 4);
  return `${Number((bytes / 1024 ** power).toFixed(2))} ${['B', 'KB', 'MB', 'GB', 'TB'][power]}`;
};

/** Build the browser index without changing replayed manifests.
 * Main callers: renderProjectOverview and renderProjectArtifacts.
 * Collect immutable snapshots, then deduplicate their producer run links. The
 * occurrence ID of an unidentified snapshot is navigation state only; it is
 * never displayed as an immutable ID or used to infer consumers.
 */
export function artifactCollections(runs) {
  const collections = new Map();
  for (const run of runs) for (const [index, artifact] of (run.artifacts || []).entries()) {
    const name = collectionName(artifact), type = artifact.artifact_type || 'artifact', key = JSON.stringify([type, name]);
    if (!collections.has(key)) collections.set(key, {key, name, type, versions:new Map()});
    const collection = collections.get(key), artifactId = recordedId(artifact), version = versionName(artifact);
    const id = artifactId === null ? `unidentified:${JSON.stringify([runIdentity(run), index])}` : JSON.stringify([artifactId, version ?? artifact.digest ?? null]);
    if (!collection.versions.has(id)) collection.versions.set(id, {id, artifactId, identityRecorded:artifactId !== null, artifact, runs:[]});
    collection.versions.get(id).runs.push(run);
  }
  return [...collections.values()].map(collection => ({...collection, versions:[...collection.versions.values()].map(version => ({...version, runs:uniqueRuns(version.runs)})).sort((a, b) => {
    const activity = String(b.artifact.created_at || b.artifact.timestamp || '').localeCompare(String(a.artifact.created_at || a.artifact.timestamp || ''));
    return activity || (versionName(b.artifact) || '').localeCompare(versionName(a.artifact) || '', undefined, {numeric:true}) || a.id.localeCompare(b.id);
  })})).sort((a, b) => a.name.localeCompare(b.name));
}

/** Resolve recorded edges for a selected immutable snapshot.
 * Main callers: Version consumer count, Usage, and Lineage tabs.
 * Match source namespace before ID and version. A use without a version is
 * conclusive only when that ID has one version in this collection. Ambiguous
 * records remain visible separately instead of creating false lineage edges.
 */
export function artifactRelationships(runs, collection, version) {
  const consumers = [], unversionedConsumers = [], producer = version.runs[0];
  const candidates = collection.versions.filter(item => item.artifactId === version.artifactId);
  if (version.identityRecorded) for (const run of runs) {
    let matched = false, ambiguous = false;
    for (const input of run.used_artifacts || []) {
      if (recordedId(input) !== version.artifactId || (input.source_entity ?? run.entity ?? '') !== (producer.entity || '') || (input.source_project ?? run.project) !== producer.project) continue;
      if (versionName(input) !== null) matched ||= versionName(input) === versionName(version.artifact);
      else if (present(input.digest)) {
        const digestMatches = candidates.filter(item => item.artifact.digest === input.digest);
        if (digestMatches.length === 1) matched ||= digestMatches[0].id === version.id;
        else if (digestMatches.length > 1) ambiguous = true;
      }
      else if (candidates.length === 1) matched = true;
      else ambiguous = true;
    }
    if (matched) consumers.push(run);
    else if (ambiguous) unversionedConsumers.push(run);
  }
  return {producers:version.runs, consumers:uniqueRuns(consumers), unversionedConsumers:uniqueRuns(unversionedConsumers)};
}

const runLinks = (runs, empty) => runs.length ? `<div class="artifact-run-links">${runs.map(run => `<p><button class="text-button" data-run="${e(runIdentity(run))}">${e(run.name || run.id)}</button> <span class="dim">${e(run.state || '')}</span></p>`).join('')}</div>` : `<p class="dim">${e(empty)}</p>`;
const ambiguityNotice = relationships => relationships.unversionedConsumers.length ? `<section><h3>Usage without a recorded version</h3><p class="dim">These runs used this artifact ID, but their records do not identify which version.</p>${runLinks(relationships.unversionedConsumers, '')}</section>` : '';

/** Show project details from recorded values and the supported public API.
 * Main callers: the project Overview destination. Runtime totals count latest
 * recorded elapsed seconds per run, not hardware compute time. Missing access
 * and contributor metadata remains explicit in the Details view.
 */
export function renderProjectOverview({project, runs, icon, date}) {
  const artifacts = artifactCollections(runs), states = [...new Set(runs.map(run => run.state))], tagsSeen = [...new Set(runs.flatMap(run => run.tags || []))];
  const timestamps = runs.map(run => run.updated_at).filter(present).sort(), lastActivity = timestamps.at(-1);
  const runtimes = runs.map(run => [run.summary?._runtime, ...(run.history || []).map(row => row._runtime)].filter(value => typeof value === 'number' && Number.isFinite(value) && value >= 0)).filter(values => values.length).map(values => values.reduce((latest, value) => Math.max(latest, value), 0));
  const contributors = Array.isArray(project.contributors) ? [...new Set(project.contributors)] : null;
  const path = [project.entity, project.name].filter(Boolean).join('/'), python = `import backprop\n\napi = backprop.Api()\nruns = api.runs(${JSON.stringify(path)})\n\nfor run in runs:\n    print(run.name, run.summary)`;
  return `<section class="project-overview"><div class="project-summary"><div><h2>${e(project.name)}</h2><p>${e(project.description || 'Experiment tracking and analysis.')}</p><p class="dim">${e(project.entity || '')}</p><button class="btn primary" data-view="workspace">${icon('grid')} Open workspace</button></div><dl>${[['Runs', runs.length], ['Metrics', metricKeys(runs).length], ['Artifact collections', artifacts.length], ['Last activity', lastActivity ? date(lastActivity) : 'No runs yet']].map(([label, value]) => `<div><dt>${e(label)}</dt><dd>${e(value)}</dd></div>`).join('')}</dl></div><section class="project-details"><h2>Details</h2>${fields([['Visibility', project.visibility], ['Last active', lastActivity ? date(lastActivity) : 'No runs yet'], ['Contributors', contributors === null ? 'Not recorded' : `${contributors.length}${contributors.length ? ' · ' + contributors.join(', ') : ''}`], ['Total runs', runs.length], ['Total compute', project.total_compute], ...(runtimes.length ? [['Recorded run time', `${Number(runtimes.reduce((sum, value) => sum + value, 0).toFixed(2))} seconds`]] : [])])}${runtimes.length ? `<p class="dim">Recorded run time sums the latest elapsed time for ${runtimes.length} of ${runs.length} runs. Hardware compute time is not recorded.</p>` : ''}</section><section><h2>Public API</h2><p class="dim">Use your Backprop configuration and Foundry credentials to inspect this project.</p><pre class="json-view"><code>${e(python)}</code></pre></section><section><h2>Runs</h2><div class="project-states">${states.map(state => `<span class="badge"><span class="dot state-dot ${e(state)}"></span>${e(state)} · ${runs.filter(run => run.state === state).length}</span>`).join('')}</div><div class="table-wrap"><table><thead><tr><th>Name</th><th>State</th><th>Group</th><th>Created</th></tr></thead><tbody>${runs.slice(0, 10).map(run => `<tr><td><button class="text-button" data-run="${e(runIdentity(run))}">${e(run.name || run.id)}</button></td><td>${e(run.state)}</td><td>${e(run.group || '')}</td><td>${e(recordedDate(run.created_at, date))}</td></tr>`).join('')}</tbody></table></div><button class="btn small" data-view="runs">View all ${runs.length} runs</button></section>${tagsSeen.length ? `<section><h2>Tags</h2><div class="project-states">${tags(tagsSeen)}</div></section>` : ''}</section>`;
}

/** Render only the selected artifact tab, keeping file actions in their owner.
 * Main callers: renderProjectArtifacts. Version gives the immutable overview;
 * Metadata exposes user metadata, Usage lists runs, Files delegates to existing
 * renderRunFiles, and Lineage arranges the recorded edges as a local path.
 */
function renderArtifactTab({tab, collection, version, relationships, date, renderFiles}) {
  const artifact = version.artifact;
  if (tab === 'metadata') return `<section><h3>Metadata</h3>${artifact.metadata && Object.keys(artifact.metadata).length ? `<pre class="json-view">${e(JSON.stringify(artifact.metadata, null, 2))}</pre>` : '<p class="dim">No metadata recorded for this version.</p>'}</section>`;
  if (tab === 'usage') return `<section><h3>Logged by</h3>${runLinks(relationships.producers, 'No producer run recorded.')}</section><section><h3>Used by</h3>${runLinks(relationships.consumers, 'No usage recorded in this project.')}</section>${ambiguityNotice(relationships)}`;
  if (tab === 'files') return renderFiles({...version.runs[0], files:[], artifacts:[artifact], used_artifacts:[], pending_artifacts:[]}, 'artifacts');
  if (tab === 'lineage') return `<p class="dim">Producer and consumer relationships recorded in this project.</p><div class="detail-overview artifact-lineage"><section><h3>Producer runs</h3>${runLinks(relationships.producers, 'No producer run recorded.')}</section><section><h3>Artifact version</h3><p class="mono" style="overflow-wrap:anywhere">${e(qualifiedName(collection, version))}</p></section><section><h3>Consumer runs</h3>${runLinks(relationships.consumers, 'No consumer runs recorded.')}</section></div>${ambiguityNotice(relationships)}`;
  const files = Array.isArray(artifact.files) ? artifact.files : null, knownSizes = (files || []).filter(file => Number.isSafeInteger(file.size) && file.size >= 0);
  const size = files === null ? 'Not recorded' : knownSizes.length === files.length ? formatBytes(knownSizes.reduce((sum, file) => sum + file.size, 0)) : `${formatBytes(knownSizes.reduce((sum, file) => sum + file.size, 0))} recorded; ${files.length - knownSizes.length} file sizes unknown`;
  return `<section><h3>Version details</h3>${fields([['Qualified name', qualifiedName(collection, version)], ['Version', versionName(artifact)], ['Artifact ID', version.artifactId], ['Aliases', markup(tags(artifact.aliases))], ['Tags', markup(tags(artifact.tags))], ['Digest', artifact.digest], ['Created by', typeof artifact.created_by === 'string' ? artifact.created_by : artifact.created_by?.username], ['Created at', recordedDate(artifact.created_at || artifact.timestamp, date)], ['Logged by', markup(runLinks(relationships.producers, 'No producer run recorded.'))], ['Consumer runs', `${relationships.consumers.length}${relationships.unversionedConsumers.length ? ` confirmed; ${relationships.unversionedConsumers.length} usage records have no version` : ''}`], ['Files', files === null ? 'Not recorded' : files.length], ['Size', size]])}<h3>Description</h3><p class="artifact-description">${e(artifact.description || 'No description recorded for this version.')}</p></section>`;
}

/** Collection -> immutable version -> selected details tab, with local return.
 * Main callers: the Artifacts destination. Search filters collections by name
 * and type without discarding selection. Parent handlers own collection,
 * version, tab and URL state; this routine stays a pure renderer.
 */
export function renderProjectArtifacts({runs, selection = {}, search = '', icon, date, renderFiles}) {
  const collections = artifactCollections(runs), matching = collections.filter(item => `${item.name} ${item.type}`.toLowerCase().includes(search.toLowerCase()));
  const current = selection.collection ? collections.find(item => item.key === selection.collection) : matching[0], selected = current?.versions.find(item => item.id === selection.version), version = selected || current?.versions[0];
  const tab = ARTIFACT_TABS.includes(selection.tab) ? selection.tab : 'version', groups = [...new Set(matching.map(item => item.type))].sort();
  const relationships = current ? artifactRelationships(runs, current, version) : null;
  const warning = current && selection.version && !selected ? '<p class="notice">The selected version is unavailable. Showing the latest recorded version.</p>' : '';
  const identityWarning = version && !version.identityRecorded ? '<p class="notice">No immutable artifact ID was recorded. This snapshot is shown separately; version and usage identity cannot be verified.</p>' : '';
  return `<div class="project-artifacts"><aside class="artifact-collections"><label class="search">${icon('search')}<input type="search" data-state="artifactSearch" aria-label="Search artifact collections" value="${e(search)}" placeholder="Search artifacts"></label>${groups.map(type => `<section class="artifact-type-group"><h3 style="margin-top:18px">${e(type)}</h3>${matching.filter(collection => collection.type === type).map(collection => `<button class="artifact-collection ${collection === current ? 'active' : ''}" ${collection === current ? 'aria-current="true"' : ''} data-artifact-collection="${e(collection.key)}"><strong>${e(collection.name)}</strong><span>${collection.versions.length} ${collection.versions.length === 1 ? 'version' : 'versions'}</span></button>`).join('')}</section>`).join('') || '<p class="dim">No matching collections.</p>'}</aside><section class="artifact-version-page">${current ? `<header class="artifact-version-heading"><div><span class="tag">${e(current.type)}</span><h2>${e(current.name)}</h2></div><label>Version<select data-artifact-version aria-label="Artifact version">${current.versions.map(item => `<option value="${e(item.id)}" ${item === version ? 'selected' : ''}>${e(versionName(item.artifact) || 'Version not recorded')}${item.artifact.aliases?.length ? ' · ' + e(item.artifact.aliases.join(', ')) : ''}</option>`).join('')}</select></label></header>${warning}${identityWarning}<nav class="dialog-tabs artifact-tabs" role="tablist" aria-label="Artifact details">${ARTIFACT_TABS.map(name => `<button class="dialog-tab ${tab === name ? 'active' : ''}" role="tab" tabindex="${tab === name ? 0 : -1}" id="artifact-tab-${name}" aria-controls="artifact-tab-content" aria-selected="${tab === name}" data-artifact-tab="${name}">${name[0].toUpperCase() + name.slice(1)}</button>`).join('')}</nav><div id="artifact-tab-content" role="tabpanel" aria-labelledby="artifact-tab-${tab}">${renderArtifactTab({tab, collection:current, version, relationships, date, renderFiles})}</div>` : `<div class="empty">${icon('data')}<h2>${collections.length ? selection.collection ? 'The selected collection is unavailable' : 'Choose an artifact collection' : 'No artifacts logged'}</h2><p>Browse versions, aliases, metadata, files and their experiment relationships.</p></div>`}</section></div>`;
}
