/**
 * Architecture: recorded artifact/run fixtures -> project-page index and tabs ->
 * identity, relationship, and rendering assertions.
 * Main caller: node --test tests/test_frontend_project_pages.mjs.
 * Reference journeys: collection -> version -> Version/Metadata/Usage/Files/
 * Lineage. File actions stay injected; missing provenance remains explicit.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import {normalizeEvents} from '../web/model.mjs';
import {ARTIFACT_TABS, artifactCollections, artifactRelationships, renderProjectOverview, renderProjectArtifacts} from '../web/project-pages.mjs';

const icon = name => `<i data-icon="${name}"></i>`;
const date = value => `Date(${value})`;
const manifest = (version, extra = {}) => ({artifact_id:'same-id', name:'dataset', artifact_type:'dataset', version, digest:`digest-${version}`, timestamp:'2026-10-07T00:00:00Z', files:[{name:'examples.table.json', size:2048}], aliases:['latest'], tags:['evaluated'], metadata:{rows:4}, description:'Evaluation examples', ...extra});
const run = (id, extra = {}) => ({key:`team/project/${id}`, id, name:id, entity:'team', project:'project', state:'finished', created_at:'2026-10-07T00:00:00Z', updated_at:'2026-10-07T00:01:00Z', config:{}, summary:{}, history:[], definitions:{}, artifacts:[], used_artifacts:[], tags:[], ...extra});
const fixture = () => {
  const first = manifest('v0'), second = manifest('v1');
  return [run('producer', {artifacts:[first, first, second]}), run('co-producer', {artifacts:[first]}), run('reader-v0', {used_artifacts:[{artifact_id:'same-id', version:'v0'}, {artifact_id:'same-id', version:'v0'}]}), run('reader-v1', {used_artifacts:[{artifact_id:'same-id', version:'v1'}]}), run('reader-unspecified', {used_artifacts:[{artifact_id:'same-id'}]}), run('reader-other-source', {used_artifacts:[{artifact_id:'same-id', version:'v0', source_project:'other'}]})];
};
const page = (runs, selection, extra = {}) => renderProjectArtifacts({runs, selection, search:'', icon, date, renderFiles:() => '<button data-file-action>Open table</button>', ...extra});

test('one artifact ID retains distinct versions and repeated logging contributes unique producer runs', () => {
  const runs = fixture(), before = JSON.stringify(runs), [collection] = artifactCollections(runs);
  assert.equal(collection.name, 'dataset');
  assert.equal(collection.versions.length, 2);
  assert.deepEqual(collection.versions.map(item => item.artifact.version), ['v1', 'v0']);
  assert.notEqual(collection.versions[0].id, collection.versions[1].id);
  assert.deepEqual(collection.versions.find(item => item.artifact.version === 'v0').runs.map(item => item.id), ['producer', 'co-producer']);
  assert.equal(JSON.stringify(runs), before);
  const suffix = artifactCollections([run('a', {artifacts:[manifest(0, {name:'dataset:v0'})]})])[0];
  assert.equal(suffix.name, 'dataset');
  assert.equal(suffix.versions[0].id, collection.versions.find(item => item.artifact.version === 'v0').id);
});

test('consumers match recorded version and source, with ambiguous inputs separate from lineage', () => {
  const runs = fixture(), [collection] = artifactCollections(runs);
  const version = collection.versions.find(item => item.artifact.version === 'v0'), relationships = artifactRelationships(runs, collection, version);
  assert.deepEqual(relationships.producers.map(item => item.id), ['producer', 'co-producer']);
  assert.deepEqual(relationships.consumers.map(item => item.id), ['reader-v0']);
  assert.deepEqual(relationships.unversionedConsumers.map(item => item.id), ['reader-unspecified']);
  const html = page(runs, {collection:collection.key, version:version.id, tab:'lineage'});
  assert.match(html, /Producer runs/); assert.match(html, /Consumer runs/);
  assert.match(html, /team\/project\/dataset:v0/);
  assert.match(html, /Usage without a recorded version/);
  assert.doesNotMatch(html, /data-run="team\/project\/reader-v1"/);
  assert.doesNotMatch(html, /data-run="team\/project\/reader-other-source"/);
});

test('unversioned consumption is conclusive for a unique ID, while shared digests remain ambiguous', () => {
  const runs = [run('producer', {artifacts:[manifest('v0', {digest:'same'})]}), run('reader', {used_artifacts:[{artifact_id:'same-id'}]}), run('digest-reader', {used_artifacts:[{artifact_id:'same-id', digest:'same'}]})];
  let [collection] = artifactCollections(runs);
  assert.deepEqual(artifactRelationships(runs, collection, collection.versions[0]).consumers.map(item => item.id), ['reader', 'digest-reader']);
  runs[0].artifacts.push(manifest('v1', {digest:'same'}));
  [collection] = artifactCollections(runs);
  const edges = artifactRelationships(runs, collection, collection.versions[0]);
  assert.deepEqual(edges.consumers, []);
  assert.deepEqual(edges.unversionedConsumers.map(item => item.id), ['reader', 'digest-reader']);
  runs[0].artifacts[1].digest = 'other';
  [collection] = artifactCollections(runs);
  assert.deepEqual(artifactRelationships(runs, collection, collection.versions.find(item => item.artifact.version === 'v0')).consumers.map(item => item.id), ['digest-reader']);
});

test('unidentified manifests remain inspectable and never merge or invent consumer identity', () => {
  const broken = manifest('v0'); delete broken.artifact_id;
  const runs = [run('a', {artifacts:[broken, structuredClone(broken)]}), run('b', {artifacts:[structuredClone(broken)]}), run('reader', {used_artifacts:[{name:'dataset', version:'v0', digest:broken.digest}]})];
  const [collection] = artifactCollections(runs);
  assert.equal(collection.versions.length, 3);
  assert.equal(new Set(collection.versions.map(item => item.id)).size, 3);
  for (const version of collection.versions) {
    assert.equal(version.identityRecorded, false);
    assert.deepEqual(artifactRelationships(runs, collection, version).consumers, []);
  }
  const html = page(runs, {collection:collection.key, version:collection.versions[0].id});
  assert.match(html, /No immutable artifact ID was recorded/);
  assert.match(html, /<label>Artifact ID<\/label><p>Not recorded<\/p>/);
  assert.doesNotMatch(html, /<p>unidentified:/);
});

test('each artifact tab has distinct content and only Files calls the existing file renderer', () => {
  const runs = fixture(), [collection] = artifactCollections(runs), version = collection.versions.find(item => item.artifact.version === 'v0'), calls = [];
  for (const tab of ARTIFACT_TABS) {
    const html = page(runs, {collection:collection.key, version:version.id, tab}, {renderFiles:(record, mode) => {calls.push({record, mode}); return '<button data-file-action>Open table</button>';}});
    assert.equal((html.match(/role="tab"/g) || []).length, 5);
    assert.match(html, new RegExp(`aria-selected="true" data-artifact-tab="${tab}"`));
    assert.match(html, new RegExp(`role="tabpanel" aria-labelledby="artifact-tab-${tab}"`));
    if (tab === 'version') {assert.match(html, /Version details/); assert.match(html, /<label>Size<\/label><p>2 KB<\/p>/); assert.match(html, /<label>Created by<\/label><p>Not recorded<\/p>/);}
    if (tab === 'metadata') {assert.match(html, /&quot;rows&quot;: 4/); assert.doesNotMatch(html, /Version details/);}
    if (tab === 'usage') {assert.match(html, /<h3>Logged by<\/h3>/); assert.match(html, /<h3>Used by<\/h3>/);}
    if (tab === 'files') assert.match(html, /data-file-action/);
    else assert.doesNotMatch(html, /data-file-action/);
  }
  assert.equal(calls.length, 1); assert.equal(calls[0].mode, 'artifacts');
  assert.equal(calls[0].record.key, 'team/project/producer');
  assert.deepEqual(calls[0].record.artifacts, [version.artifact]);
  for (const field of ['files', 'used_artifacts', 'pending_artifacts']) assert.deepEqual(calls[0].record[field], []);
});

test('collection navigation is grouped by type, search preserves selection, and unavailable selection is explicit', () => {
  const runs = fixture(); runs.push(run('model', {artifacts:[manifest('v10', {name:'classifier', artifact_type:'model'})]}));
  const collection = artifactCollections(runs).find(item => item.name === 'dataset');
  let html = page(runs, {collection:collection.key}, {search:'MODEL'});
  assert.match(html, /artifact-type-group/); assert.match(html, /<h3[^>]*>model<\/h3>/);
  assert.match(html, /<strong>classifier<\/strong>/); assert.doesNotMatch(html, /<strong>dataset<\/strong>/);
  assert.match(html, /<h2>dataset<\/h2>/); assert.match(html, /Version details/);
  html = page(runs, {collection:collection.key, version:'old-id', tab:'usage'});
  assert.match(html, /The selected version is unavailable/); assert.match(html, /aria-selected="true" data-artifact-tab="usage"/);
  assert.match(page(runs, {collection:'gone'}), /The selected collection is unavailable/);
  assert.match(page([], {}), /No artifacts logged/);
  assert.match(page(runs, {}), /Version details/);
  assert.match(page(runs, {}), /tabindex="0" id="artifact-tab-version"/);
});

test('Version exposes recorded provenance, counts, description and partial sizes without claiming unknown bytes', () => {
  const runs = [run('producer', {artifacts:[manifest('v0', {created_by:{username:'alice'}, created_at:'2026-10-06T00:00:00Z', files:[{name:'a', size:0}, {name:'b'}]})]})];
  const [collection] = artifactCollections(runs), html = page(runs, {collection:collection.key});
  for (const label of ['Qualified name', 'Version', 'Artifact ID', 'Aliases', 'Tags', 'Digest', 'Created by', 'Created at', 'Logged by', 'Consumer runs', 'Files', 'Size']) assert.match(html, new RegExp(`<label>${label}</label>`));
  assert.match(html, /<label>Created by<\/label><p>alice<\/p>/);
  assert.match(html, /Date\(2026-10-06T00:00:00Z\)/);
  assert.match(html, /0 B recorded; 1 file sizes unknown/);
  assert.match(html, /Evaluation examples/);
});

test('project Details and API examples use recorded scope, with missing access/account/compute metadata explicit', () => {
  const events = [
    {event_id:'start', entity:'team', project:'project', run_id:'a', type:'run_start', timestamp:'2026-10-07T00:00:00Z', payload:{name:'a'}},
    {event_id:'history', entity:'team', project:'project', run_id:'a', type:'history', timestamp:'2026-10-07T00:00:10Z', payload:{step:0, metrics:{loss:1}}},
  ];
  const model = normalizeEvents(events), html = renderProjectOverview({project:model.projects[0], runs:model.runs, icon, date});
  for (const label of ['Visibility', 'Contributors', 'Total compute']) assert.match(html, new RegExp(`<label>${label}</label><p>Not recorded</p>`));
  assert.match(html, /<label>Total runs<\/label><p>1<\/p>/);
  assert.match(html, /<label>Recorded run time<\/label><p>10 seconds<\/p>/);
  assert.match(html, /Hardware compute time is not recorded/);
  assert.match(html, /api = backprop.Api\(\)/); assert.match(html, /api.runs\(&quot;team\/project&quot;\)/);
  assert.match(html, /data-view="workspace"/); assert.match(html, /data-view="runs"/);
  const explicit = renderProjectOverview({project:{name:'project', entity:'team', visibility:'Private', contributors:['alice', 'alice', 'bob'], total_compute:0}, runs:[], icon, date});
  assert.match(explicit, /<label>Visibility<\/label><p>Private<\/p>/);
  assert.match(explicit, /<label>Contributors<\/label><p>2 · alice, bob<\/p>/);
  assert.match(explicit, /<label>Total compute<\/label><p>0<\/p>/);
});

test('recorded text and arbitrary object properties never become trusted HTML', () => {
  const artifact = manifest('v0', {name:'<dataset>', metadata:{html:'<img onerror="fail">'}, aliases:['<latest>'], description:'<script>bad()</script>', created_by:{html:'<img onerror="fail">'}});
  const runs = [run('producer', {name:'<producer>', artifacts:[artifact]})], [collection] = artifactCollections(runs);
  const html = page(runs, {collection:collection.key});
  assert.match(html, /&lt;dataset&gt;/); assert.match(html, /&lt;latest&gt;/); assert.match(html, /&lt;producer&gt;/); assert.match(html, /&lt;script&gt;bad\(\)&lt;\/script&gt;/);
  assert.doesNotMatch(html, /<script>|<img/);
  assert.doesNotMatch(page(runs, {collection:collection.key, tab:'metadata'}), /<img/);
  assert.doesNotMatch(renderProjectOverview({project:{name:'<project>', total_compute:{html:'<img onerror="fail">'}}, runs:[], icon, date}), /<img/);
});
