/**
 * Architecture: live_public_refresh.py snapshot -> actual browser reducer ->
 * compare public fields with the independent SDK read under repeated delivery.
 * Main caller: combined release verification with a saved snapshot path.
 * This reads only synthetic saved events and does not contact Foundry.
 */
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {normalizeEvents} from '../web/model.mjs';

const fixture = JSON.parse(await readFile(process.argv[2], 'utf8'));
const plain = value => JSON.parse(JSON.stringify(value));
for (const events of [fixture.events, [...fixture.events].reverse(), [...fixture.events, ...fixture.events]]) {
  const model = normalizeEvents(events);
  assert.deepEqual(model.issues, []);
  assert.equal(model.runs.length, 1);
  const run = model.runs[0];
  const config = Object.fromEntries(Object.entries(run.config).filter(([key]) => !['_wandb', 'wandb_version'].includes(key)));
  assert.equal(run.id, fixture.run_id);
  assert.equal(run.name, fixture.attrs.displayName);
  assert.equal(run.state, fixture.attrs.state);
  assert.deepEqual(plain(config), fixture.attrs.config);
  assert.deepEqual(plain(run.summary), fixture.attrs.summaryMetrics);
  assert.deepEqual(plain(run.history), fixture.history);
  assert.deepEqual(plain(run.system.at(-1) || {}), fixture.attrs.systemMetrics);
}
console.log(JSON.stringify({sdk_version: fixture.sdk_version, run_id: fixture.run_id,
  delivery_orders: 3, matched: ['name', 'state', 'config', 'summary', 'history', 'system_metrics']}));
