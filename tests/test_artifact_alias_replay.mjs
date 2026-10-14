/** Actual alias event ownership must agree in Python and the browser model. */
import test from 'node:test';
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';
import {normalizeEvents} from '../web/model.mjs';
test('alias ownership survives delayed visibility, stale edits and duplicate replay', () => {
  const data = JSON.parse(execFileSync(process.env.BACKPROP_PYTHON || 'python3',
    ['-B','tests/artifact_alias_replay_fixture.py'], {encoding:'utf8',env:{...process.env,PYTHONPATH:'sdk'}}));
  const delayed = normalizeEvents(data.delayed_events);
  assert.deepEqual(delayed.issues, []);
  assert.equal(delayed.runs.length, 1);
  assert(delayed.runs[0].artifacts[0].aliases.includes('latest'));
  const view = normalizeEvents([...data.events].reverse().concat(data.events));
  assert.deepEqual(view.issues, []);
  const artifacts = view.runs.flatMap(run => run.artifacts);
  assert.equal(artifacts.length, 2);
  for (const artifact of artifacts) assert.deepEqual(artifact.aliases, data.python_aliases[artifact.artifact_id]);
  const first = artifacts.find(a => a.artifact_id === data.first_id);
  const second = artifacts.find(a => a.artifact_id === data.second_id);
  assert(first.aliases.includes(''));
  assert(first.aliases.includes('v1'));
  assert(!first.aliases.includes('best'));
  assert(second.aliases.includes('latest'));
  assert(second.aliases.includes('best'));
  assert.equal(data.resolved[first.version], first.artifact_id);
});
