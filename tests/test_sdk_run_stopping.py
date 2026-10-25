"""Portable remote stopping contract and bounded Foundry stream boundary.

Tests use synthetic MemorySink runs and a subprocess for real SIGINT. No user
processes or live Foundry runs are ever targeted. Main callers: unittest discover.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import uuid
import warnings

import backprop
from backprop.control_store import FoundryControlReader
from backprop.foundry import FoundryStore
from backprop.transport import read_journal


def request_event(run, *, instance=None, identifier=None, **overrides):
    return {"schema_version": 1, "event_id": identifier or uuid.uuid4().hex,
            "timestamp": "2099-01-01T00:00:00+00:00", "project": run.project,
            "entity": run.entity, "run_id": run.id, "type": "run_stop_request",
            "payload": {"action": "stop", "instance_id": instance or run._stop_checker.instance_id},
            **overrides}


def wait_for(predicate, timeout=2):
    end = time.monotonic() + timeout
    while not predicate() and time.monotonic() < end:
        time.sleep(.005)
    return bool(predicate())


class RunStoppingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {'BACKPROP_DIR': self.temp.name}).start()
        patch('backprop.run_stopping.POLL_INTERVAL', .01).start()
        self.sink = backprop.MemorySink()
        self.calls = []
        self.runs = []
        self.addCleanup(self.finish_runs)

    def finish_runs(self):
        for run in self.runs:
            if not run._closed:
                run._error = None
                run.finish()

    def new(self, **kwargs):
        settings = {"console": "off", "quiet": True, "flush_interval": .02, "stop_fn": lambda: self.calls.append(threading.current_thread().ident)}
        settings.update(kwargs.pop('settings', {}))
        run = backprop.Run(project='control-test', entity='test', dir=self.temp.name,
                           settings=settings, _sink=self.sink, _deployment={}, **kwargs)
        self.runs.append(run)
        return run

    def test_public_run_stop_uses_connected_process_without_changing_state(self):
        from backprop.apis.public import Api
        run = self.new()
        api = Api._from_store(self.sink, entity='test', project='control-test')
        api.run(run.id).stop()
        self.assertTrue(wait_for(lambda: bool(self.calls)))
        self.assertFalse(run._closed)
        self.assertEqual(api.run(run.id).state,'running')
        self.assertEqual(len(self.calls),1)

    def test_remote_callback_once_with_durable_ack_and_separate_finished_state(self):
        run = self.new()
        started = next(e for e in self.sink.events if e['type'] == 'run_start')
        self.assertTrue(started['payload']['control']['stop_supported'])
        event = request_event(run)
        self.sink.publish([event, event, request_event(run)])
        self.assertTrue(wait_for(lambda: bool(self.calls)))
        self.assertNotEqual(self.calls[0], threading.main_thread().ident)
        self.assertFalse(run._closed)
        self.assertFalse(run._stop_checker.trigger())  # fatal and remote share one gate
        records = read_journal(run.sync_dir)
        ack = [e for e in records if e['type'] == 'run_stop_ack']
        self.assertEqual([e['payload']['status'] for e in ack], ['accepted'])
        self.assertEqual(ack[0]['payload']['request_id'], event['event_id'])
        self.assertEqual(len(self.calls), 1)
        self.assertFalse(any(e['type'] == 'run_finish' for e in records))
        run.finish()
        self.assertTrue(any(e['type'] == 'run_stop_ack' for e in self.sink.events))

    def test_resumed_process_rejects_old_instance_and_other_scope_requests(self):
        first = self.new(id='reuse')
        old = request_event(first)
        first.finish()
        second = self.new(id='reuse', resume='must')
        self.assertNotEqual(first._stop_checker.instance_id, second._stop_checker.instance_id)
        invalid = [old, request_event(second, project='other'), request_event(second, entity='other'),
                   request_event(second, run_id='other'), request_event(second, type='history'),
                   request_event(second, payload={'action':'not-stop','instance_id':second._stop_checker.instance_id})]
        self.sink.publish(invalid)
        time.sleep(.04)
        self.assertFalse(self.calls)
        self.sink.publish([request_event(second)])
        self.assertTrue(wait_for(lambda: len(self.calls) == 1))

    def test_offline_and_disabled_never_create_control_reader(self):
        with patch.object(self.sink, 'control_reader', side_effect=AssertionError('must not read')):
            for mode in ('offline', 'disabled'):
                run = self.new(mode=mode)
                self.assertIsNone(run._stop_checker.reader)
                self.assertIsNone(run._stop_checker._thread)
                self.assertFalse(any(e.get('payload',{}).get('control') for e in read_journal(run.sync_dir)))
                run.finish()
        self.assertFalse(self.calls)

    def test_finish_does_not_wait_for_read_or_invoke_late_callback(self):
        entered, release = threading.Event(), threading.Event()
        class Reader:
            def poll(reader):
                entered.set(); release.wait(2)
                return [request_event(self.runs[0])]
        with patch.object(self.sink, 'control_reader', return_value=Reader()):
            run = self.new(settings={'finish_timeout': .1})
        self.assertTrue(entered.wait(1))
        started = time.monotonic()
        run.finish()
        self.assertLess(time.monotonic()-started, .3)
        release.set()
        run._stop_checker._thread.join(1)
        self.assertFalse(self.calls)

    def test_reader_errors_retry_without_losing_logging_or_stop(self):
        with warnings.catch_warnings(record=True) as captured:
            run = self.new()
            original = run._stop_checker.reader.poll
            count = 0
            def poll():
                nonlocal count
                count += 1
                if count < 3:
                    raise ConnectionError('private detail should not be logged')
                return original()
            run._stop_checker.reader.poll = poll
            self.sink.publish([request_event(run)])
            run.log({'still_logging': 1})
            self.assertTrue(wait_for(lambda: bool(self.calls)))
        self.assertGreaterEqual(count, 3)
        self.assertIsNone(run._error)
        text = ' '.join(str(w.message) for w in captured)
        self.assertIn('stop-request check failed', text)
        self.assertNotIn('private detail', text)

    def test_callback_error_acknowledged_once_without_exception_detail(self):
        def broken():
            self.calls.append(1)
            raise ValueError('private callback content')
        with warnings.catch_warnings(record=True):
            run = self.new(settings={'stop_fn': broken})
            self.sink.publish([request_event(run)])
            self.assertTrue(wait_for(lambda: any(e['type']=='run_stop_ack' and e['payload']['status']=='failed' for e in read_journal(run.sync_dir))))
        ack = [e['payload'] for e in read_journal(run.sync_dir) if e['type']=='run_stop_ack']
        self.assertEqual([e['status'] for e in ack], ['accepted', 'failed'])
        self.assertEqual(ack[-1]['error'], 'ValueError')
        self.assertNotIn('private callback content', json.dumps(ack))
        self.assertFalse(run._stop_checker.trigger())
        self.assertEqual(self.calls, [1])

    def test_ack_append_failure_retries_same_request_before_callback(self):
        run = self.new()
        original = run._journal.append
        attempts = 0
        def append(event):
            nonlocal attempts
            if event['type'] == 'run_stop_ack':
                attempts += 1
                if attempts == 1:
                    raise OSError('disk transient')
            return original(event)
        with patch.object(run._journal, 'append', side_effect=append), warnings.catch_warnings(record=True):
            event = request_event(run)
            self.sink.publish([event])
            self.assertTrue(wait_for(lambda: bool(self.calls)))
        self.assertEqual(attempts, 2)
        self.assertEqual(len([e for e in read_journal(run.sync_dir) if e['type']=='run_stop_ack']), 1)

    def test_control_prime_failure_does_not_publish_capability_or_leave_worker(self):
        with patch.object(self.sink, 'control_reader', side_effect=ConnectionError('control denied')):
            with self.assertRaises(ConnectionError):
                self.new()
        self.assertFalse(self.sink.events)
        records = [json.loads(line) for path in Path(self.temp.name).rglob('events.jsonl') for line in path.read_text().splitlines()]
        self.assertFalse(any(event['type'] == 'run_start' for event in records))

    def test_prime_happens_before_start_publish_and_first_check_sees_request(self):
        original = self.sink.publish
        def publish(events):
            original(events)
            for event in events:
                if event['type']=='run_start':
                    original([{**event, 'event_id':uuid.uuid4().hex, 'type':'run_stop_request',
                               'payload':{'instance_id':event['payload']['control']['instance_id'], 'action':'stop'}}])
        with patch.object(self.sink, 'publish', side_effect=publish):
            run = self.new()
            self.assertTrue(wait_for(lambda: bool(self.calls)))

    def test_stop_reader_remains_independent_of_blocked_upload(self):
        run = self.new()
        entered, release = threading.Event(), threading.Event()
        original = self.sink.publish
        def slow(events):
            entered.set()
            release.wait(2)
            original(events)
        with patch.object(self.sink, 'publish', side_effect=slow):
            run.log({'loss':1})
            run._wake.set()
            self.assertTrue(entered.wait(1))
            original([request_event(run)])
            try:
                self.assertTrue(wait_for(lambda: bool(self.calls)))
            finally:
                release.set()
                run._thread.join(.1)

    def test_remote_then_actual_fatal_failure_does_not_repeat_callback(self):
        run = self.new(settings={'stop_on_fatal_error':True})
        self.sink.publish([request_event(run)])
        self.assertTrue(wait_for(lambda: bool(self.calls)))
        with patch.object(self.sink, 'publish', side_effect=ValueError('permanent')), warnings.catch_warnings(record=True):
            run.log({'loss':1})
            run._wake.set()
            self.assertTrue(wait_for(lambda: run._error is not None))
            run._thread.join(1)
        self.assertEqual(len(self.calls),1)
        run._error=None

    def test_default_remote_stop_interrupts_run_in_isolated_process(self):
        script = r"""
import tempfile,time,threading,uuid
import backprop
import backprop.run_stopping
backprop.run_stopping.POLL_INTERVAL=.01
with tempfile.TemporaryDirectory() as directory:
    sink=backprop.MemorySink()
    run=backprop.Run(project='isolated-stop-test',dir=directory,
        settings={'quiet':True,'console':'off'},_sink=sink,_deployment={})
    request={'schema_version':1,'event_id':uuid.uuid4().hex,'timestamp':'2099',
        'type':'run_stop_request','project':run.project,'entity':run.entity,'run_id':run.id,
        'payload':{'action':'stop','instance_id':run._stop_checker.instance_id}}
    threading.Timer(.05,lambda:sink.publish([request])).start()
    started=time.monotonic()
    try:
        time.sleep(5)
    except KeyboardInterrupt:
        assert time.monotonic()-started<1
        run.finish(exit_code=1)
        assert any(e['type']=='run_stop_ack' for e in sink.events)
        print('REMOTE_SIGINT_DELIVERED')
    else:
        raise AssertionError('remote stop did not interrupt main thread')
"""
        result=subprocess.run([sys.executable,'-B','-c',script],env={**os.environ,'PYTHONPATH':str(Path(backprop.__file__).parents[1])},capture_output=True,text=True,timeout=3)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('REMOTE_SIGINT_DELIVERED',result.stdout)

    def test_default_sigint_wakes_main_thread_sleep_in_isolated_process(self):
        script = '''
import time, threading
from backprop.run_stopping import interrupt_main
threading.Timer(.05, interrupt_main).start()
started=time.monotonic()
try:
    time.sleep(5)
except KeyboardInterrupt:
    assert time.monotonic()-started < 1
    print('SIGINT_DELIVERED')
else:
    raise AssertionError('main thread was not interrupted')
'''
        result = subprocess.run([sys.executable, '-B', '-c', script], env={**os.environ, 'PYTHONPATH':str(Path(backprop.__file__).parents[1])}, capture_output=True, text=True, timeout=3)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('SIGINT_DELIVERED', result.stdout)


class FakeControlStore:
    _sql_text = staticmethod(FoundryStore._sql_text)
    record_to_event = staticmethod(FoundryStore.record_to_event)
    stream_path='/stream'; stream_rid='ri.foundry.main.dataset.test'
    def __init__(self):
        self.ends={'0':'10'}; self.rows={}; self.requests=[]; self.archive=[]; self.sql=[]
    def _stream_query(self, **kwargs):
        from urllib.parse import urlencode
        return urlencode(kwargs)
    def request(self, path):
        from urllib.parse import parse_qs, urlsplit
        self.requests.append(path)
        if 'getEndOffsets' in path:
            return dict(self.ends)
        query=parse_qs(urlsplit(path).query)
        key=query['partitionId'][0]; offset=int(query['startOffset'][0])
        return [row for row in self.rows.get(key,[]) if int(row['offset']) >= offset][:int(query['limit'][0])]
    def _query_csv(self, sql, **kwargs):
        self.sql.append((sql, kwargs)); return list(self.archive)


class ControlStoreTests(unittest.TestCase):
    def setUp(self):
        self.store=FakeControlStore()
        self.scope={'project':"p'quoted",'entity':'e','run_id':'r','instance_id':'a'*32}
        source=type('Source',(),{'config':{'timeout':60},'token':'private'})()
        with patch('backprop_backend.connection.create_store', return_value=self.store):
            self.reader=FoundryControlReader(source, **self.scope)
    def event(self, **changes):
        return {'schema_version':1,'event_id':'id','timestamp':'2026','type':'run_stop_request',
                'project':self.scope['project'],'entity':'e','run_id':'r',
                'payload':{'instance_id':'a'*32,'action':'stop'},**changes}
    def row(self, offset, event):
        return {'offset':str(offset),'value':FoundryStore.event_to_record(event)}

    def test_offset_prime_sparse_pages_new_partition_and_exact_matching(self):
        self.assertEqual(self.reader.offsets, {'0':10})
        self.store.ends={'0':'40','new':'4'}
        self.store.rows={'0':[self.row(5,self.event()), self.row(12,self.event(type='history')),
                                self.row(25,self.event(project='other'))],
                         'new':[self.row(3,self.event())]}
        self.assertEqual(self.reader.poll(), [self.event()])
        self.assertEqual(self.reader.offsets['0'], 26)
        self.assertEqual(self.reader.offsets['new'], 4)
        self.assertFalse(self.store.sql)

    def test_work_is_bounded_and_partition_service_rotates(self):
        self.reader._page_size=1;self.reader._max_pages=2
        self.store.ends={str(i):'3' for i in range(5)}
        self.store.rows={str(i):[self.row(0,self.event(type='history')), self.row(1,self.event(type='history'))] for i in range(5)}
        self.reader.offsets={}
        self.reader.poll(); self.reader.poll()
        pages=[r for r in self.store.requests if 'getRecords' in r]
        self.assertEqual(len(pages),4)
        self.assertTrue(any('partitionId=2' in p for p in pages))

    def test_archive_recovery_is_scoped_bounded_and_not_every_poll(self):
        self.store.archive=[FoundryStore.event_to_record(self.event())]
        self.reader._last_archive -= 301
        self.assertEqual(self.reader.poll(), [self.event()])
        sql,options=self.store.sql[0]
        self.assertIn('event_type =',sql);self.assertIn('payload LIKE',sql);self.assertIn('LIMIT 1000',sql)
        self.assertNotIn("p'quoted",sql)
        self.assertEqual(options,{'timeout':10,'retry':False})
        self.assertEqual(self.reader.poll(),[])
        self.assertEqual(len(self.store.sql),1)

    def test_error_preserves_cursor_and_triggers_archive_recovery(self):
        old=dict(self.reader.offsets)
        with patch.object(self.store,'request', side_effect=ConnectionError('read failed')):
            with self.assertRaises(ConnectionError): self.reader.poll()
        self.assertEqual(self.reader.offsets,old)
        self.store.archive=[FoundryStore.event_to_record(self.event())]
        self.assertEqual(self.reader.poll(),[self.event()])
        self.assertEqual(len(self.store.sql),1)

    def test_archive_recovers_while_stale_live_offset_keeps_failing(self):
        self.store.ends={'0':'100'}
        original=self.store.request
        def request(path):
            if 'getRecords' in path:
                raise ValueError('expired offset')
            return original(path)
        self.store.request=request
        with self.assertRaises(ValueError): self.reader.poll()
        self.store.archive=[FoundryStore.event_to_record(self.event())]
        self.assertEqual(self.reader.poll(),[self.event()])
        self.assertEqual(self.reader.offsets,{'0':10})
        self.assertEqual(len(self.store.sql),1)

    def test_stop_beyond_captured_end_is_retained_for_next_poll(self):
        self.store.ends={'0':'12'}
        self.store.rows={'0':[self.row(15,self.event())]}
        self.assertEqual(self.reader.poll(),[])
        self.assertEqual(self.reader.offsets['0'],12)
        self.store.ends={'0':'16'}
        self.assertEqual(self.reader.poll(),[self.event()])


if __name__ == '__main__':
    unittest.main()
