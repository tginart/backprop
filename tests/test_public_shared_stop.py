"""Shared process stopping through durable per-instance control requests.

Architecture: genuine shared journals -> public stop -> independent control
readers -> callbacks and acknowledgements. Main caller: SDK CI. Delivery tests
exercise retries and delayed snapshots without signalling any external process.
"""
import copy
import multiprocessing
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import backprop
from backprop.control_store import active_stop_controls
from backprop.transport import read_journal, sync


def process_writer(directory, events, state, label, primary):
    """Run one real logging handle with its own thread, journal and callback.

    Main caller: the independent-process test. Only the test store's event list
    crosses process boundaries; SDK control readers and journal state do not.
    """
    import backprop.run_stopping
    backprop.run_stopping.POLL_INTERVAL = .01
    sink = backprop.MemorySink()
    sink.events = events
    with backprop.Run(project='stop', entity='team', id='shared', mode='shared',
            dir=directory, _sink=sink, settings={
                'x_label': label, 'x_primary': primary, 'silent': True, 'console': 'off',
                'x_disable_stats': True, 'disable_git': True, 'disable_code': True,
                'stop_fn': lambda: state.__setitem__(label + '-called', True)}) as run:
        state[label + '-ready'] = run._stop_checker.instance_id
        deadline = time.monotonic() + 15
        while not state.get(label + '-finish'):
            if time.monotonic() >= deadline: raise TimeoutError('test parent did not finish writer')
            time.sleep(.01)
        state[label + '-acks'] = len([event for event in read_journal(run.sync_dir)
                                    if event['type'] == 'run_stop_ack'])


class PublicSharedStop(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {'BACKPROP_DIR': self.temp.name}).start()
        patch('backprop.run_stopping.POLL_INTERVAL', .005).start()
        self.sink = backprop.MemorySink()
        self.api = backprop.Api._from_store(self.sink, entity='team', project='stop')
        self.runs, self.calls = [], []
        self.addCleanup(self.close)

    def close(self):
        for run in self.runs:
            if not run._closed: run.finish()

    def start(self, label, primary=False):
        run = backprop.Run(project='stop', entity='team', id='shared', mode='shared',
            dir=self.temp.name, _sink=self.sink, settings={
                'x_label': label, 'x_primary': primary, 'silent': True, 'console': 'off',
                'x_disable_stats': True, 'disable_git': True,
                'stop_fn': lambda: self.calls.append(label)})
        self.runs.append(run)
        return run

    def await_calls(self, expected):
        deadline = time.monotonic() + 3
        while len(self.calls) < len(expected) and time.monotonic() < deadline:
            time.sleep(.005)
        self.assertCountEqual(self.calls, expected)

    def test_all_observed_live_writers_receive_one_request_and_keep_running_until_finish(self):
        primary = self.start('primary', True)
        secondary = self.start('secondary')
        departed = self.start('departed'); departed.finish()
        reader = self.api.run('shared')
        self.assertIsNone(reader.stop())
        self.await_calls(['primary', 'secondary'])
        self.assertEqual(reader.state, 'running')
        self.assertFalse(primary._closed); self.assertFalse(secondary._closed)
        self.assertIsNone(reader.stop())
        requests = [event for event in self.sink.events if event['type'] == 'run_stop_request']
        self.assertEqual(len(requests), 2)
        self.assertEqual({event['payload']['instance_id'] for event in requests},
                         {run._stop_checker.instance_id for run in (primary, secondary)})
        for run in (primary, secondary):
            records = read_journal(run.sync_dir)
            self.assertEqual(len([event for event in records if event['type'] == 'run_stop_ack']), 1)
        secondary.finish()
        self.assertEqual(self.api.run('shared').state, 'running')
        primary.finish()
        self.api.flush()
        self.assertEqual(self.api.run('shared').state, 'finished')
        reader.stop()
        self.assertEqual(len([event for event in self.sink.events if event['type'] == 'run_stop_request']), 2)

    def test_failed_publish_is_retried_durably_before_duplicate_suppression(self):
        self.start('primary', True)
        self.start('secondary')
        reader = self.api.run('shared')
        original = self.sink.publish
        attempts = []
        def fail_once(events):
            requests = [event for event in events if event['type'] == 'run_stop_request']
            if requests:
                attempts.extend(copy.deepcopy(requests))
                if len(attempts) == 1: raise ConnectionError('temporary control failure')
            original(events)
        with patch.object(self.sink, 'publish', side_effect=fail_once):
            with self.assertRaises(backprop.Error): reader.stop()
            reader.stop()
        self.await_calls(['primary', 'secondary'])
        self.assertEqual(attempts[0]['event_id'], attempts[1]['event_id'])
        writer = self.api._writers[('team', 'stop', 'shared')]
        requests = [event for event in read_journal(writer._journal.directory) if event['type'] == 'run_stop_request']
        self.assertEqual(len(requests), 2)

    def test_delayed_new_writer_needs_a_fresh_call_and_receives_no_old_request(self):
        self.start('primary', True)
        reader = self.api.run('shared')
        reader.stop(); self.await_calls(['primary'])
        late = self.start('late')
        time.sleep(.025)
        self.assertEqual(self.calls, ['primary'])
        original = self.sink.read_events
        hidden = lambda **scope: [event for event in original(**scope)
                                  if event['payload'].get('_writer', {}).get('id') != late._writer_id]
        with patch.object(self.sink, 'read_events', side_effect=hidden): reader.stop()
        self.assertEqual(self.calls, ['primary'])
        reader.stop(); self.await_calls(['primary', 'late'])

    def test_failed_request_recovers_from_its_journal_in_a_fresh_client(self):
        self.start('primary', True)
        reader = self.api.run('shared')
        with patch.object(self.sink, 'publish', side_effect=ConnectionError('temporarily unavailable')):
            with self.assertRaises(backprop.Error): reader.stop()
        directory = self.api._writers[('team', 'stop', 'shared')]._journal.directory
        records = read_journal(directory)
        identifier = next(event['event_id'] for event in records if event['type'] == 'run_stop_request')
        # Recover using the on-disk journal, without the original mutation
        # writer or public object. The normal CLI sync uses this same function.
        sync(directory, sink=self.sink)
        fresh = backprop.Api._from_store(self.sink, entity='team', project='stop')
        fresh.run('shared').stop()
        sync(directory, sink=self.sink)
        self.await_calls(['primary'])
        self.assertEqual([event['event_id'] for event in self.sink.events
                          if event['type'] == 'run_stop_request'], [identifier])

    def test_target_reconstruction_is_order_independent_and_rejects_conflicting_ids(self):
        primary = self.start('primary', True)
        secondary = self.start('secondary')
        stopped = self.start('stopped'); stopped.finish()
        events = copy.deepcopy(self.sink.events)
        expected = {run._stop_checker.instance_id for run in (primary, secondary)}
        for delivery in (events, events[::-1], events[::-1] + events):
            self.assertEqual({control['instance_id'] for control in active_stop_controls(delivery)}, expected)
        corrupt = copy.deepcopy(events[0]); corrupt['payload']['control']['instance_id'] = 'conflict'
        with self.assertRaisesRegex(ValueError, 'Conflicting records'):
            active_stop_controls([*events, corrupt])

    def test_independent_processes_receive_the_same_public_stop(self):
        context = multiprocessing.get_context('spawn')
        with context.Manager() as manager:
            events, state = manager.list(), manager.dict()
            sink = backprop.MemorySink(); sink.events = events
            api = backprop.Api._from_store(sink, entity='team', project='stop')
            processes = [context.Process(target=process_writer,
                args=(self.temp.name, events, state, label, label == 'primary'))
                for label in ('primary', 'secondary')]
            def wait_for(keys):
                deadline = time.monotonic() + 10
                while not all(state.get(key) for key in keys) and time.monotonic() < deadline:
                    self.assertTrue(all(process.is_alive() for process in processes))
                    time.sleep(.01)
                self.assertTrue(all(state.get(key) for key in keys), dict(state))
            try:
                for process in processes: process.start()
                wait_for(['primary-ready', 'secondary-ready'])
                reader = api.run('shared')
                reader.stop()
                wait_for(['primary-called', 'secondary-called'])
                reader.stop()
                self.assertEqual(len([event for event in events if event['type'] == 'run_stop_request']), 2)
                state['secondary-finish'] = True
                processes[1].join(5); self.assertEqual(processes[1].exitcode, 0)
                reader.load(force=True); self.assertEqual(reader.state, 'running')
                state['primary-finish'] = True
                processes[0].join(5); self.assertEqual(processes[0].exitcode, 0)
                reader.load(force=True); self.assertEqual(reader.state, 'finished')
                self.assertEqual(state['primary-acks'], 1)
                self.assertEqual(state['secondary-acks'], 1)
                snapshot = {'events': list(events), 'expected': {
                    'state': reader.state,
                    'writers': {key: value['state'] for key, value in reader._state['writers'].items()},
                    'primary_instance': state['primary-ready'],
                    'requested_instances': sorted([state['primary-ready'], state['secondary-ready']])}}
                output = Path(__file__).resolve().parents[1] / 'test-results/public-shared-stop-replay.json'
                output.parent.mkdir(exist_ok=True)
                output.write_text(json.dumps(snapshot, indent=2))
            finally:
                for process in processes:
                    if process.is_alive(): process.terminate()
                    if process.pid is not None: process.join(5)


if __name__ == '__main__': unittest.main()
