"""Failed run refreshes must retain one complete previous public snapshot.

Architecture: real local logging and publication -> retained public handle ->
invalid newer source event -> failed refresh -> unchanged edits and read state.
Main callers: SDK contract verification. These tests model conflicting delivery
and damaged rich-value encodings without a hosted service or package install.
"""
import copy
import os
import tempfile
import unittest
import uuid
from unittest.mock import patch

import backprop
from backprop.apis.public import Api


class AtomicRunRefresh(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        environment = patch.dict(os.environ, {'BACKPROP_DIR': self.temp.name})
        environment.start()
        self.addCleanup(environment.stop)
        self.sink = backprop.MemorySink()
        with backprop.Run(id='atomic', entity='e', project='refresh', dir=self.temp.name,
                          config={'value': 1}, _sink=self.sink, _deployment={},
                          settings={'console': 'off', 'silent': True, 'disable_git': True,
                                    'disable_code': True, 'x_disable_stats': True}) as run:
            run.log({'loss': 1})
        self.api = Api._from_store(self.sink, entity='e', project='refresh')
        self.reader = self.api.run('atomic')

    def assert_retained_after_error(self, event):
        """A refresh error must preserve editable values and their baselines.

        Main callers: each invalid-event regression below. Retaining only the
        visible summary is insufficient: future writes also use the raw events
        and the original config/metadata baselines.
        """
        reader = self.reader
        reader.config['unsaved'] = 2
        reader.name = 'unsaved name'
        fields = ('_events', '_state', '_config_edit', '_config_before',
                  '_metadata_before', '_blobs')
        identities = {field: getattr(reader, field) for field in fields}
        before = copy.deepcopy(identities)
        self.sink.publish([event])
        with self.assertRaises(ValueError):
            reader.load(force=True)
        for field in fields:
            self.assertIs(getattr(reader, field), identities[field], field)
            self.assertEqual(getattr(reader, field), before[field], field)
        self.assertEqual(reader.summary['loss'], 1)
        self.assertEqual(reader.config['unsaved'], 2)
        self.assertEqual(reader.name, 'unsaved name')

    def malformed_value_event(self, kind):
        event = copy.deepcopy(self.sink.events[-1])
        event.update(event_id=uuid.uuid4().hex, type=kind,
                     timestamp='2099-01-01T00:00:00+00:00',
                     payload={'values': {'damaged': {'_type': 'timestamp', 'value': 'not-a-date'}}})
        return event

    def test_conflicting_duplicate_does_not_replace_raw_events(self):
        event = copy.deepcopy(next(e for e in self.sink.events if e['type'] == 'history'))
        event['payload']['metrics']['loss'] = 2
        self.assert_retained_after_error(event)

    def test_invalid_config_does_not_replace_replayed_state(self):
        self.assert_retained_after_error(self.malformed_value_event('config'))

    def test_invalid_returned_summary_does_not_commit_other_valid_fields(self):
        self.assert_retained_after_error(self.malformed_value_event('summary'))

    def test_invalid_system_and_history_projection_do_not_download_or_commit(self):
        for kind in ('system', 'history'):
            with self.subTest(kind=kind):
                event = self.malformed_value_event(kind)
                event['payload'] = {'step': 1, 'metrics': event['payload']['values'],
                                    'summary': {'loss': 1}}
                with patch.object(self.sink, 'download_file', side_effect=AssertionError('projection downloaded a blob')):
                    self.assert_retained_after_error(event)
                self.sink.events.pop()

    def test_failed_initial_load_can_retry_from_an_uninitialized_handle(self):
        from backprop.apis.public.runs import Run as PublicRun
        reader = PublicRun(self.api, 'e', 'refresh', 'atomic')
        self.sink.publish([self.malformed_value_event('config')])
        with self.assertRaises(ValueError):
            reader.load()
        self.assertIsNone(reader._events)
        self.assertIsNone(reader._state)
        self.assertNotIn('_config_before', reader.__dict__)
        self.sink.events.pop()
        self.assertEqual(reader.load()['config'], {'value': 1})

    def test_held_summary_and_unsaved_edits_follow_successful_publication(self):
        reader = self.reader
        summary = reader.summary
        reader.config['unsaved'] = 2
        reader.name = 'unsaved name'
        summary['reviewed'] = True
        self.assertEqual(reader.config['unsaved'], 2)
        self.assertEqual(reader.name, 'unsaved name')
        self.assertNotIn('unsaved', reader._config_before)
        self.assertNotEqual(reader._metadata_before['name'], reader.name)
        reader.update()
        attrs = reader.load(force=True)
        self.assertEqual(attrs['config']['unsaved'], 2)
        self.assertEqual(attrs['displayName'], 'unsaved name')
        self.assertTrue(summary['reviewed'])
        attrs['config']['unsaved'] = 99
        attrs['tags'].append('detached')
        self.assertEqual(reader.config['unsaved'], 2)
        self.assertNotIn('detached', reader.tags)

    def test_successful_refresh_replaces_all_fields_together(self):
        reader = self.reader
        old_events, old_state = reader._events, reader._state
        event = copy.deepcopy(self.sink.events[-1])
        event.update(event_id=uuid.uuid4().hex, type='run_edit',
                     timestamp='2099-01-01T00:00:00+00:00',
                     payload={'config': {'values': {'value': 2}},
                              'metadata': {'values': {'name': 'new name'}}})
        self.sink.publish([event])
        attrs = reader.load(force=True)
        self.assertIsNot(reader._events, old_events)
        self.assertIsNot(reader._state, old_state)
        self.assertEqual(attrs['config']['value'], 2)
        self.assertEqual(attrs['displayName'], 'new name')
        self.assertEqual(reader._config_before, reader.config)
        self.assertEqual(reader._metadata_before, reader._state['metadata'])


if __name__ == '__main__':
    unittest.main()
