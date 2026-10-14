/** Real constructor metadata must survive the production browser event model. */
import test from 'node:test';
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';
import {normalizeEvents} from '../web/model.mjs';
test('artifact descriptive JSON survives retry, edits and reordered duplicate events', () => {
  const fixture = JSON.parse(execFileSync(process.env.BACKPROP_PYTHON || 'python3',
    ['-B','tests/artifact_constructor_replay_fixture.py'], {encoding:'utf8',env:{...process.env,PYTHONPATH:'sdk'}}));
  const view = normalizeEvents([...fixture.events].reverse().concat(fixture.events));
  assert.deepEqual(view.issues, []);
  assert.equal(view.runs.length, 1);
  const artifact = view.runs[0].artifacts.find(value => value.artifact_id === fixture.artifact_id);
  assert(artifact);
  assert.deepEqual(artifact.metadata, fixture.expected);
  assert.deepEqual(artifact.metadata, fixture.python_metadata);
  assert.equal(artifact.version, fixture.version);
  assert.equal(artifact.description, 'updated');
  assert.equal(fixture.stable_version, true);
  assert.equal(fixture.immutable_contents_verified, true);
});
