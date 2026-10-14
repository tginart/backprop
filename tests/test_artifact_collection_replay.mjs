/** Actual Python collection edits must reconstruct identically in the UI. */
import test from 'node:test';
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';
import {normalizeEvents} from '../web/model.mjs';
import {collectionCatalog} from '../web/artifact-collections.mjs';
test('collection edits retain contents, aliases, types and identity across duplicated replay', () => {
  const data = JSON.parse(execFileSync(process.env.BACKPROP_PYTHON || 'python3',
    ['-B','tests/artifact_collection_fixture.py'], {encoding:'utf8', env:{...process.env,PYTHONPATH:'sdk'}}));
  const model = normalizeEvents([...data.events].reverse().concat(data.events));
  assert.deepEqual(model.issues, []);
  const actual = model.runs.flatMap(run => run.artifacts.map(a => ({run_id:run.id,
    artifact_id:a.artifact_id, name:a.name, artifact_type:a.artifact_type, version:a.version, aliases:a.aliases})));
  const order = (a,b) => a.artifact_id.localeCompare(b.artifact_id);
  assert.deepEqual(actual.sort(order), data.expected.sort(order));
  const used = model.runs.find(run => run.id === 'consumer').used_artifacts[0];
  assert.match(used.name, /^trained-weights:/);
  assert.equal(used.artifact_type, 'checkpoint');
  const edited = data.events.filter(event => event.type === 'collection_update');
  assert.throws(() => collectionCatalog([edited[0]]), /no visible original/);
});
