/** Compare the browser with actual independently stopped Python writers.
 * Main caller: combined Node CI, after the Python SDK tests create the fixture.
 * The selected primary's dialog status is separate from every writer's state.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync, existsSync} from 'node:fs';
import {normalizeEvents} from '../web/model.mjs';

const path = process.env.BACKPROP_SHARED_STOP_FIXTURE || new URL('../test-results/public-shared-stop-replay.json', import.meta.url);
test('shared stop events agree with the SDK after reversed and repeated delivery', {
  skip: !existsSync(path) && !process.env.CI ? 'Run test_public_shared_stop.py first' : false,
}, () => {
  const {events, expected} = JSON.parse(readFileSync(path, 'utf8'));
  for (const delivery of [events, [...events].reverse(), [...events].reverse().concat(events)]) {
    const model = normalizeEvents(delivery);
    assert.deepEqual(model.issues, []);
    const run = model.runs[0];
    assert.equal(run.state, expected.state);
    assert.deepEqual(Object.fromEntries(Object.entries(run.writers).map(([key, value]) => [key, value.state])), expected.writers);
    assert.equal(run.control.instance_id, expected.primary_instance);
    assert.equal(run.stop_requests.length, 1);
    assert.equal(run.stop_requests[0].status, 'accepted');
    assert.equal(run.stop_requests[0].instance_id, expected.primary_instance);
    assert.deepEqual([...new Set(events.filter(event => event.type === 'run_stop_request').map(event => event.payload.instance_id))].sort(), expected.requested_instances);
  }
});
