/** Imported source identity -> semantic content check -> one visible row.
 * Main caller: normalizeEvents, after physical event/writer accounting. Ordinary
 * logging is unaffected; only versioned TensorBoard records can be collapsed.
 * Keep this rule identical to sdk/backprop/tensorboard_replay.py.
 */
import { sha256 } from './sha256.mjs';
import { projectGroups } from './tensorboard-groups.mjs';

const fields = ['event_sha256', 'filename', 'namespace', 'occurrence', 'version'];
const markers = new Set(['_tensorboard_record', '_tensorboard_origin', '_tensorboard_source', '_runtime', '_step']);
const placement = new Set(['path', 'name', 'dataset_rid', 'transaction_rid', 'transaction_id', '_backprop_local_file']);

export function originIdentity(origin) {
  if (!origin || typeof origin !== 'object' || Array.isArray(origin) || JSON.stringify(Object.keys(origin).sort()) !== JSON.stringify(fields))
    throw new Error('TensorBoard origin must contain exactly the version-one fields');
  if (origin.version !== 1) throw new Error('TensorBoard origin version must be 1');
  if (typeof origin.namespace !== 'string' || origin.namespace.includes('\0')) throw new Error('Invalid TensorBoard origin namespace');
  if (typeof origin.filename !== 'string' || !origin.filename || ['.', '..'].includes(origin.filename)
      || origin.filename.includes('/') || origin.filename.includes('\0')) throw new Error('Invalid TensorBoard origin filename');
  // Python's UTF-8 encoder rejects unpaired surrogates. TextEncoder replaces
  // them with U+FFFD, which would merge different malformed source identities.
  // Unicode mode treats valid surrogate pairs as a single allowed code point.
  if (/[\uD800-\uDFFF]/u.test(origin.namespace) || /[\uD800-\uDFFF]/u.test(origin.filename))
    throw new Error('TensorBoard source labels must contain valid Unicode');
  if (typeof origin.event_sha256 !== 'string' || !/^[a-f0-9]{64}$/.test(origin.event_sha256)) throw new Error('Invalid TensorBoard origin event digest');
  if (!Number.isSafeInteger(origin.occurrence) || origin.occurrence < 0) throw new Error('Invalid TensorBoard origin occurrence');
  const prefix = new TextEncoder().encode(`${origin.namespace}\0${origin.filename}\0${origin.occurrence}\0`);
  const bytes = new Uint8Array(prefix.length + 32); bytes.set(prefix);
  bytes.set(Uint8Array.from(origin.event_sha256.match(/../g), value => parseInt(value, 16)), prefix.length);
  return sha256(bytes);
}

export function semanticValue(value) {
  if (Array.isArray(value)) return value.map(semanticValue);
  if (!value || typeof value !== 'object') return value;
  const ignored = new Set();
  if (typeof value.sha256 === 'string' && /^[a-f0-9]{64}$/.test(value.sha256)
      && Number.isSafeInteger(value.size) && value.size >= 0 && ('path' in value || '_backprop_local_file' in value))
    for (const field of placement) ignored.add(field);
  if (value._type === 'table' && 'data' in value) { ignored.add('table_id'); ignored.add('artifact_path'); }
  return Object.fromEntries(Object.keys(value).sort().filter(key => !ignored.has(key)).map(key => [key, semanticValue(value[key])]));
}

export class TensorBoardHistory {
  constructor(events=[]) { this.seen = new Map(); this.cachedRows = new Map(); Object.assign(this,projectGroups(events)); }

  rows(event) {
    if(this.cachedRows.has(event.event_id)) return this.cachedRows.get(event.event_id);
    const rows = this.replacements.has(event.event_id) ? this.replacements.get(event.event_id)
      : this.consumed.has(event.event_id) || !this.accept(event) ? [] : [event.payload.metrics || {}];
    this.cachedRows.set(event.event_id, rows);
    return rows;
  }

  accept(event) {
    const metrics = event.payload.metrics || {};
    if (event.type !== 'history' || !Object.hasOwn(metrics, '_tensorboard_origin')) return true;
    const identifier = originIdentity(metrics._tensorboard_origin);
    if (metrics._tensorboard_record !== identifier) throw new Error('TensorBoard record identity does not match its origin');
    const key = JSON.stringify([event.entity || '', event.project, event.run_id, identifier]);
    const content = JSON.stringify(semanticValue(Object.fromEntries(Object.entries(metrics).filter(([key]) => !markers.has(key)))));
    if (this.seen.has(key)) {
      if (this.seen.get(key) !== content) throw new Error(`Conflicting TensorBoard observations for source record: ${identifier}`);
      return false;
    }
    this.seen.set(key, content);
    return true;
  }
}
