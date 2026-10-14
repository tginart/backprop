/** Shared export/read completeness from already visible writer records.
 * Architecture: writer-local sequences -> gaps/conflicts -> provisional writers.
 * Main callers: model.mjs and Python/browser replay fixtures. Finish can arrive
 * before earlier records. Deduplicate retries by event ID and never infer a
 * complete chain from recorded run state alone. No scheduler or leader election.
 */
export function incompleteWriters(writers, events) {
  const sequences = new Map(), invalid = new Set();
  for (const event of events) {
    const writer = event.payload._writer;
    if (!writer) continue;
    const id = writer.id, sequence = writer.sequence;
    if (!Number.isSafeInteger(sequence) || sequence < 0) { invalid.add(id); continue; }
    if (!sequences.has(id)) sequences.set(id, new Map());
    const observed = sequences.get(id);
    if (!observed.has(sequence)) observed.set(sequence, new Map());
    observed.get(sequence).set(event.event_id, event.type);
  }
  const result = [];
  for (const [id, writer] of Object.entries(writers)) {
    const observed = sequences.get(id) || new Map();
    let last = -1;
    for (const sequence of observed.keys()) last = Math.max(last, sequence);
    const missing = last + 1 - observed.size;
    const ambiguous = invalid.has(id) || [...observed.values()].some(records => records.size !== 1);
    const finishedLast = observed.size > 0 && [...observed.get(last).values()].every(kind => ['run_finish', 'writer_finish'].includes(kind));
    if (['running', 'pending'].includes(writer.state) || missing || ambiguous || !finishedLast)
      result.push({id, label: writer.label ?? id, state: writer.state, missing_records: missing,
                   invalid_sequence: ambiguous, finish_is_last: finishedLast});
  }
  return result;
}

export function completionText(writer) {
  if (!writer) return 'Complete';
  if (writer.invalid_sequence) return 'Record order needs verification';
  if (writer.missing_records) return `${writer.missing_records} record${writer.missing_records === 1 ? '' : 's'} not visible yet`;
  return 'Awaiting completion';
}
