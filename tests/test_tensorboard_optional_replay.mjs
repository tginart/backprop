/** Genuine CI imports -> browser/Python replay -> content-integrity boundaries.
 * Main caller: optional-training CI. Valid duplicate source groups retain both
 * physical writers. Root/member disagreement is malformed evidence; two valid
 * contributions with different decoded values are an immutable-source conflict.
 * Why this exists: those failures happen at distinct validation stages and must
 * not be hidden behind one broad or outdated error assertion.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { createHash } from 'node:crypto';
import { spawnSync } from 'node:child_process';
import path from 'node:path';
import { normalizeEvents } from '../web/model.mjs';
import { validateGroup } from '../web/tensorboard-groups.mjs';
import { originIdentity } from '../web/tensorboard-replay.mjs';

function groupRevision(group) {
  // Main caller: the valid-conflict fixture below. Use native SHA-256 to check
  // the approved source-identity protocol independently of browser hashing.
  // Decoded metric values are compared separately and do not change this hash.
  const digest = createHash('sha256').update('tensorboard-group-v1\0');
  for (const member of group.prefix) digest.update(Buffer.from(originIdentity(member.origin), 'hex'));
  digest.update('\0members\0');
  for (const member of group.members) digest.update(Buffer.from(originIdentity(member.origin), 'hex'));
  digest.update('\0next\0');
  if (group.next !== null) digest.update(Buffer.from(originIdentity(group.next.origin), 'hex'));
  return digest.digest('hex');
}

function pythonOutcome(events) {
  // Main caller: the exact-error comparisons. Replay the same genuine event
  // envelopes and modified evidence through Python; no rich blobs are hydrated.
  const source = `import json, sys
from backprop.apis.public.replay import reduce_events
try:
    state = reduce_events(json.load(sys.stdin))
    print(json.dumps({'history_count': len(state['history']), 'loss': state['history'][0]['loss']}))
except ValueError as error:
    print(json.dumps({'error': str(error)}))
`;
  const child = spawnSync(process.env.BACKPROP_PYTHON || 'python3', ['-B', '-c', source], {
    encoding: 'utf8', input: JSON.stringify(events), env: {...process.env, PYTHONPATH: 'sdk'}, timeout: 60000,
  });
  assert.equal(child.status, 0, child.stderr);
  return JSON.parse(child.stdout);
}

function assertReplayError(events, message) {
  // Main caller: malformed-evidence and valid-conflict checks. Delivery order
  // must not change which integrity boundary rejects the event stream.
  for (const delivered of [events, [...events].reverse(), [...events, ...events]]) {
    assert.throws(() => normalizeEvents(delivered), {message});
    assert.deepEqual(pythonOutcome(delivered), {error: message});
  }
}

for (const variant of ['', '-large']) {
const filename = path.join(process.env.BACKPROP_TENSORBOARD_REPLAY_FIXTURE_DIR || 'test-results',
  `tensorboard-gap-delayed-duplicates${variant}.json`);
test(`real imports${variant} with delayed shared visibility have one browser observation`, {skip: !fs.existsSync(filename)}, () => {
  const fixture = JSON.parse(fs.readFileSync(filename, 'utf8'));
  assert.equal(fixture.physical_history_count, 2);
  for (const events of [fixture.events, [...fixture.events].reverse(), [...fixture.events, ...fixture.events]]) {
    const run = normalizeEvents(events).runs.find(run => run.id === 'tb-duplicate');
    assert.ok(run);
    assert.equal(run.history.length, fixture.expected_reconstructed_history_count);
    assert.equal(Object.keys(run.writers).length, 2);
    assert.deepEqual(Object.values(run.writers).map(writer => writer.state), ['finished', 'finished']);
    assert.equal(run.history_incomplete_writers.length, 0);
    assert.equal(run.history[0].loss, .25);
    assert.equal(run.history[0].global_step, 8);
    assert.equal(run.history[0].frame[0].width, 2);
    assert.equal(run.history[0].curve.kind, 'line');
    if (variant) {
      const table = run.history[0].curve.table;
      assert.equal(table.external, true);
      const data = Buffer.from(fixture.files[table.path], 'hex');
      assert.equal(data.length, table.size);
      assert.equal(createHash('sha256').update(data).digest('hex'), table.sha256);
      assert.equal(JSON.parse(data).data.length, 6000);
    }
  }
  const malformed = structuredClone(fixture.events);
  malformed.find(event => event.type === 'history').payload.metrics.loss = .99;
  assertReplayError(malformed, 'TensorBoard group metrics disagree with member evidence');

  const conflict = structuredClone(fixture.events);
  const changed = conflict.filter(event => event.type === 'history').at(-1);
  const metrics = changed.payload.metrics, group = metrics._tensorboard_group;
  const winning = [...group.members].reverse().find(member => Object.hasOwn(member.metrics, 'loss'));
  assert.ok(winning);
  metrics.loss = .99;
  winning.metrics.loss = .99;
  const previousRevision = group.revision;
  group.revision = groupRevision(group);
  assert.equal(group.revision, previousRevision);
  validateGroup(group, metrics._tensorboard_record, metrics._tensorboard_origin);
  const contribution = conflict.filter(event => event.payload._writer?.id === changed.payload._writer.id);
  const single = normalizeEvents(contribution).runs.find(run => run.id === 'tb-duplicate');
  assert.equal(single.history.length, 1);
  assert.equal(single.history[0].loss, .99);
  assert.deepEqual(pythonOutcome(contribution), {history_count: 1, loss: .99});
  assertReplayError(conflict, 'Conflicting TensorBoard member metrics');
});
}
