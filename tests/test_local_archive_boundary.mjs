/**
 * Purpose: Join a local archive snapshot to its live tail without gaps or replay.
 * Input: A complete archive, concurrent writes, and the real browser client.
 * Output: Only records after the immutable snapshot boundary cross the live API.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import {createClient} from '../web/backends/http.js';

const CONFIG = {
    backend: 'local', host: 'http://127.0.0.1:8765',
    app_url: 'http://127.0.0.1:8765/', local_id: 'a'.repeat(32), branch: 'master',
    stream_dataset_rid: 'ri.foundry.local.dataset.events',
    media_dataset_rid: 'ri.foundry.local.dataset.media',
};
const FIELDS = ['schema_version', 'event_id', 'run_id', 'project', 'entity',
    'event_type', 'timestamp', 'payload'];

function record(offset) {
    return {schema_version: 1, event_id: `event-${offset}`, run_id: 'run',
        project: 'project', entity: 'local', event_type: 'history',
        timestamp: '2026-10-21T06:00:00Z', payload: JSON.stringify({metrics: {loss: offset}})};
}

function csv(records) {
    const cell = value => '"' + String(value).replaceAll('"', '""') + '"';
    return FIELDS.join(',') + '\n' + records.map(row =>
        FIELDS.map(field => cell(row[field])).join(',')).join('\n');
}

test('local snapshots seed live offsets across concurrent writes and forced refreshes', async () => {
    const originalFetch = globalThis.fetch, originalLocation = globalThis.location;
    const records = Array.from({length: 1200}, (_, offset) => record(offset));
    const queries = new Map(), starts = [];
    globalThis.location = {origin: CONFIG.host, href: CONFIG.app_url};
    globalThis.fetch = async (address, options = {}) => {
        if (address === './config.json') return Response.json(CONFIG);
        const url = new URL(address);
        if (url.pathname.endsWith('/execute')) {
            const id = String(queries.size), end = records.length;
            queries.set(id, end);
            return Response.json({queryId: id, type: 'succeeded'});
        }
        if (url.pathname.endsWith('/getResults')) {
            const id = url.pathname.split('/').at(-2), end = queries.get(id);
            const content = csv(records.slice(0, end));
            records.push(record(records.length));
            return new Response(content, {headers: {'X-Backprop-End-Offset': String(end)}});
        }
        if (url.pathname.endsWith('/getEndOffsets')) return Response.json({'0': String(records.length)});
        if (url.pathname.endsWith('/getRecords')) {
            const start = Number(url.searchParams.get('startOffset') || 0);
            starts.push(start);
            return Response.json(records.slice(start).map((value, index) => ({
                offset: String(start + index), value,
            })));
        }
        throw new Error(`Unexpected request: ${options.method} ${url.pathname}`);
    };
    try {
        const api = createClient();
        await api.initialize();
        const initial = await api.loadWorkspace();
        assert.equal(initial.events.length, 1201);
        assert.deepEqual(starts, [1200]);
        const refreshed = await api.loadWorkspace({refreshArchive: true});
        assert.equal(refreshed.events.length, 1202);
        assert.deepEqual(starts, [1200, 1201]);
        await api.loadRunControl({id: 'run', project: 'project', entity: 'local'}, {refreshArchive: true});
        assert.deepEqual(starts, [1200, 1201, 1202]);
    } finally {
        globalThis.fetch = originalFetch;
        globalThis.location = originalLocation;
    }
});
