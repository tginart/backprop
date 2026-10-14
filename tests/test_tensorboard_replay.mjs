/** Shared Python/browser fixtures validate identities, content and SHA bytes. */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { spawnSync } from 'node:child_process';
import { sha256 } from '../web/sha256.mjs';
import { TensorBoardHistory, originIdentity } from '../web/tensorboard-replay.mjs';
import { normalizeEvents } from '../web/model.mjs';

test('synchronous SHA-256 agrees with native crypto across padding boundaries and UTF-8', () => {
  const inputs = ['', 'abc', 'validation\0source\0🧪', ...[55, 56, 63, 64, 65, 127, 128, 10000].map(n => 'x'.repeat(n))];
  for (const input of inputs) {
    const bytes = new TextEncoder().encode(input);
    assert.equal(sha256(bytes), createHash('sha256').update(bytes).digest('hex'));
  }
  const allBytes = Uint8Array.from({length: 256}, (_, index) => index);
  assert.equal(sha256(allBytes), createHash('sha256').update(allBytes).digest('hex'));
});

test('browser and Python retain the same imported observations and reject every conflict', () => {
  const child = spawnSync(process.env.BACKPROP_PYTHON || 'python3', ['-B', 'tests/tensorboard_replay_fixture.py'], {
    encoding: 'utf8', env: {...process.env, PYTHONPATH: 'sdk'}, timeout: 60000,
  });
  assert.equal(child.status, 0, child.stderr);
  const fixture = JSON.parse(child.stdout);
  for (const [name, events] of Object.entries(fixture.cases)) {
    const replay = new TensorBoardHistory(); let actual;
    try { actual = {accepted: events.filter(event => replay.accept(event)).map(event => event.event_id)}; }
    catch { actual = {error: true}; }
    assert.deepEqual(actual, fixture.expected[name], name);
  }
  const events = fixture.cases.duplicate;
  for (const input of [events, [...events].reverse(), [...events, ...events]]) {
    const result = normalizeEvents(input).runs[0];
    assert.equal(result.history.length, 1);
    assert.equal(Object.keys(result.writers).length, 2);
    assert.equal(result.eventCount, 2);
  }
  assert.throws(() => normalizeEvents(fixture.cases['conflict-scalar']), /Conflicting TensorBoard/);
  const origin = {...events[0].payload.metrics._tensorboard_origin, namespace: 'valid-🧪', filename: 'source-🧪'};
  const prefix = new TextEncoder().encode(`${origin.namespace}\0${origin.filename}\0${origin.occurrence}\0`);
  assert.equal(originIdentity(origin), createHash('sha256').update(prefix).update(Buffer.from(origin.event_sha256, 'hex')).digest('hex'));
});
