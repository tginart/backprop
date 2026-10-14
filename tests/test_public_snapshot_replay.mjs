/**
 * Architecture: genuine Run journals -> public SDK snapshots -> browser replay.
 * Ordinary, explicit and fork/resume times must survive reordered deliveries.
 * Main caller: focused Node verification and the combined release test suite.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';
import {normalizeEvents} from '../web/model.mjs';

test('ordinary and explicitly timed history match public reads after duplicate and reversed delivery', () => {
  const fixture = JSON.parse(execFileSync(process.env.BACKPROP_PYTHON || 'python3', ['-B', '-c', `
import json,tempfile
import backprop as sdk
with tempfile.TemporaryDirectory() as directory:
    sink=sdk.MemorySink()
    with sdk.Run(id='times',project='snapshot',entity='fixture',dir=directory,_sink=sink,_deployment={},
                 settings={'console':'off','silent':True,'disable_git':True,'disable_code':True,'x_disable_stats':True}) as run:
        run.log({'loss':3})
        run.log({'loss':2,'_timestamp':1700000000.123456,'_runtime':17.25})
    # Choose deterministic microseconds not representable by JS Date.parse.
    start=next(event for event in sink.events if event['type']=='run_start')
    start['timestamp']='2026-10-07T01:00:00.123001+00:00'
    for event in sink.events:
        if event['type']=='history':
            event['timestamp']=f"2026-10-07T01:00:0{1+event['payload']['step']}.456789+00:00"
        elif event is not start:
            event['timestamp']='2026-10-07T01:00:03.000001+00:00'
    reader=sdk.Api._from_store(sink,entity='fixture',project='snapshot').run('times')
    print(json.dumps({'events':sink.events,'history':reader.history(pandas=False)}))
`], {encoding:'utf8', env:{...process.env,PYTHONPATH:'sdk'}}));
  assert.equal(fixture.history[0]._timestamp, 1791334801.456789);
  assert.equal(fixture.history[1]._timestamp, 1700000000.123456);
  assert.equal(fixture.history[1]._runtime, 17.25);
  for (const events of [fixture.events, [...fixture.events].reverse(), [...fixture.events, ...fixture.events]]) {
    const model = normalizeEvents(events);
    assert.deepEqual(model.issues, []);
    assert.deepEqual(JSON.parse(JSON.stringify(model.runs[0].history)), fixture.history);
  }
});

test('fork history waits for its actual start and resume keeps that first runtime origin', () => {
  const fixtures = JSON.parse(execFileSync(process.env.BACKPROP_PYTHON || 'python3', ['-B', '-c', `
import json,tempfile
from datetime import datetime,timedelta,timezone
import backprop as sdk
with tempfile.TemporaryDirectory() as directory:
    sink=sdk.MemorySink()
    settings={'console':'off','silent':True,'disable_git':True,'disable_code':True,'x_disable_stats':True}
    common={'project':'snapshot','entity':'fixture','dir':directory,'_sink':sink,'_deployment':{}}
    with sdk.Run(id='parent',settings=settings,**common) as parent:
        parent.log({'loss':3})
        parent.log({'loss':2})
    with sdk.Run(id='child',settings={**settings,'fork_from':'parent?_step=1'},**common) as child:
        child.log({'loss':1})
        child._emit('system',{'step':0,'metrics':{'cpu':12}})
    fork_count=sum(event['run_id']=='child' for event in sink.events)
    with sdk.Run(id='child',resume='allow',settings=settings,**common) as resumed:
        resumed.log({'loss':0})
        resumed._emit('system',{'step':1,'metrics':{'cpu':23}})
    events=[event for event in sink.events if event['run_id']=='child']
    # Preserve the actual emitted lifecycle order, assigning reproducible
    # microseconds so both early history and resumed start origins are distinct.
    origin=datetime(2026,10,7,1,0,0,123001,tzinfo=timezone.utc)
    for index,event in enumerate(events):
        event['timestamp']=(origin+timedelta(seconds=index,microseconds=index)).isoformat()
    def snapshot(records):
        """Read a lifecycle prefix through the public SDK without editing rows.

        Main callers: finished-fork and finished-resume snapshots below.
        """
        store=sdk.MemorySink()
        store.events=records
        reader=sdk.Api._from_store(store,entity='fixture',project='snapshot').run('child')
        return {'events':records,'history':reader.history(pandas=False),
                'system':reader.history(pandas=False,stream='system'),
                'createdAt':reader.load_full_data()['createdAt']}
    print(json.dumps([snapshot(events[:fork_count]),snapshot(events)]))
`], {encoding:'utf8', env:{...process.env,PYTHONPATH:'sdk'}}));
  const [fork, resumed] = fixtures;
  assert.deepEqual(fork.events.slice(0, 3).map(event => event.type), ['history', 'history', 'run_start']);
  const starts = resumed.events.filter(event => event.type === 'run_start');
  assert.equal(starts.length, 2);
  assert.notEqual(starts[0].timestamp, starts[1].timestamp);
  assert.equal(fork.createdAt, starts[0].timestamp);
  assert.equal(resumed.createdAt, starts[0].timestamp);
  assert.equal(fork.history.length, 3);
  assert.equal(resumed.history.length, 4);
  for (const fixture of fixtures) {
    for (const row of fixture.history.slice(0, 2)) assert.equal(Object.hasOwn(row, '_runtime'), false);
    for (const row of fixture.history.slice(2)) assert.equal(Object.hasOwn(row, '_runtime'), true);
    for (const events of [fixture.events, [...fixture.events].reverse(), [...fixture.events, ...fixture.events]]) {
      const model = normalizeEvents(events);
      assert.deepEqual(model.issues, []);
      assert.equal(model.runs.length, 1);
      const run = model.runs[0];
      assert.equal(run.created_at, fixture.createdAt);
      assert.equal(Object.hasOwn(run, '_startedAt'), false);
      assert.deepEqual(JSON.parse(JSON.stringify(run.history)), fixture.history);
      assert.deepEqual(JSON.parse(JSON.stringify(run.system)), fixture.system);
    }
  }
});
