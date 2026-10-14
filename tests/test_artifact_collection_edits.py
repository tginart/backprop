"""Collection save contract, durable retry recovery and deterministic conflicts.

Architecture: real SDK file publication -> independent public editors -> stream
and journal replay. No server identity or mutable collection database is added.
"""
import copy
import json
import os
from pathlib import Path
import tempfile
import subprocess
import sys
import unittest
from unittest.mock import patch

import backprop
from backprop.apis.public import Api
from backprop.artifact_collections import collection_catalog
from backprop.artifact_updates import artifact_views
from backprop.transport import Journal, read_journal
from artifact_collection_fixture import fixture


class CollectionEdits(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.environment = patch.dict(os.environ, {'BACKPROP_DIR': self.temp.name})
        self.environment.start(); self.addCleanup(self.environment.stop)
        self.store = backprop.MemorySink()
        self.api = Api._from_store(self.store, entity='e', project='p')
        with backprop.Run(entity='e', project='p', id='r', dir=self.temp.name, _sink=self.store,
                          settings={'silent': True, 'console': 'off'}) as run:
            for name in ('one', 'other'):
                artifact = backprop.Artifact(name, 'model')
                with artifact.new_file('data') as file: file.write(name)
                run.log_artifact(artifact)

    def test_empty_lazy_collection_defers_reads_and_cannot_invent_persistence(self):
        from backprop.apis.public.artifacts import ArtifactCollection
        with patch.object(self.store, 'read_events', side_effect=AssertionError('eager read')):
            empty = ArtifactCollection(self.api, 'e', 'p', 'absent', 'model', [])
            self.assertIsNone(empty.description)
            self.assertIsNone(empty.created_at)
            self.assertEqual(empty.tags, [])
        before = copy.deepcopy(self.store.events)
        empty.description = 'changed'
        with self.assertRaisesRegex(backprop.errors.CommError, 'no visible original'):
            empty.save()
        self.assertEqual(self.store.events, before)

    def test_rename_type_stale_edit_and_immutable_relationships(self):
        result = fixture()
        self.assertEqual(result['collection']['description'], 'reviewed weights')
        self.assertEqual(result['collection']['tags'], ['approved'])
        expected = artifact_views(result['events'])
        self.assertEqual(artifact_views(list(reversed(result['events'])) + result['events']), expected)
        self.assertEqual({e['payload']['name'] for e in expected}, {'trained-weights'})

    def test_acknowledgement_loss_retries_same_identity_and_restart_flushes(self):
        collection = self.api.artifact_collection('model', 'one')
        original = self.store.publish
        def fail(events): original(events); raise OSError('lost acknowledgement')
        collection.name = 'renamed'
        with patch.object(self.store, 'publish', side_effect=fail):
            with self.assertRaises(Exception): collection.save()
        writer = self.api._writers[('e', 'p', '__project__')]
        directory = writer._journal.directory
        pending = read_journal(directory)
        self.assertEqual(len(pending), 1)
        Journal(directory, self.store).flush()
        fresh = Api._from_store(self.store, entity='e', project='p')
        self.assertEqual(fresh.artifact_collection('model', 'renamed').id, collection.id)
        collection.save()
        updates = [e for e in self.store.events if e['type'] == 'collection_update']
        self.assertEqual(len({e['event_id'] for e in updates}), 1)
        self.assertFalse(fresh.artifact_collection_exists('one', 'model'))

    def test_reserved_type_readonly_scope_validation_and_occupied_destination(self):
        collection = self.api.artifact_collection('model', 'one')
        before = len(self.store.events)
        for field in ('entity', 'project'):
            with self.assertRaises(AttributeError): setattr(collection, field, 'elsewhere')
        for value in ('bad/name', 'a' * 129, 2):
            with self.assertRaises((TypeError, ValueError)): collection.name = value
        for value in ('job', 'wandb-internal'):
            collection.type = value
            with self.assertRaises(Exception): collection.save()
        collection.type, collection.name = 'model', 'other'
        with self.assertRaises(Exception): collection.save()
        self.assertEqual(len(self.store.events), before)

    def test_reusing_old_name_starts_distinct_collection_and_alias_scope(self):
        old = self.api.artifact_collection('model', 'one')
        old.name = 'renamed'; old.save()
        with backprop.Run(entity='e', project='p', id='new', dir=self.temp.name, _sink=self.store,
                          settings={'silent': True, 'console': 'off'}) as run:
            artifact = backprop.Artifact('one', 'model')
            with artifact.new_file('data') as file: file.write('different version')
            run.log_artifact(artifact)
        new = self.api.artifact_collection('model', 'one')
        self.assertNotEqual(old.id, new.id)
        self.assertNotEqual(self.api.artifact('one:latest').id, self.api.artifact('renamed:latest').id)

    def test_conflicting_concurrent_destinations_and_unknown_original_fail_visibly(self):
        first = self.api.artifact_collection('model', 'one')
        second = self.api.artifact_collection('model', 'other')
        first.name = 'destination'; first.save()
        event = copy.deepcopy(self.store.events[-1])
        event['event_id'] += '-conflict'
        event['payload'].update(collection_id=second.id, name='other')
        with self.assertRaisesRegex(ValueError, 'destination already exists'):
            collection_catalog([*self.store.events, event])
        with self.assertRaisesRegex(ValueError, 'no visible original'):
            collection_catalog([event])

    def test_actual_pinned_save_mutations_return_values_and_rejections(self):
        from importlib.metadata import version, PackageNotFoundError
        try: installed = version('wandb')
        except PackageNotFoundError: self.skipTest('Pinned W&B reference is unavailable')
        self.assertEqual(installed, '0.30.0')
        root = Path(__file__).resolve().parents[1]
        outcomes = {}
        for implementation in ('reference','native'):
            env = {key:value for key,value in os.environ.items() if not key.startswith(('WANDB_', 'BACKPROP_'))}
            env['PYTHONPATH'] = str(root/'sdk') if implementation == 'native' else ''
            process = subprocess.run([sys.executable, '-B', str(root/'tests/artifact_collection_reference_probe.py'), implementation],
                env=env, cwd=self.temp.name, capture_output=True, text=True, timeout=60)
            self.assertEqual(process.returncode, 0, process.stderr)
            outcomes[implementation] = json.loads(process.stdout)
        evidence = root/'test-results'/'artifact-collection-save-reference.json'
        evidence.parent.mkdir(exist_ok=True)
        evidence.write_text(json.dumps(outcomes, indent=2)+'\n')
        self.assertEqual(outcomes['native']['outputs'], outcomes['reference']['outputs'])
        for implementation, output in outcomes.items():
            self.assertTrue(all(item['type'] for item in output['failures']), implementation)
            self.assertEqual([item['type'] for item in output['failures'][:2]], ['AttributeError','AttributeError'])

    def test_pending_distributed_handle_resolves_after_collection_rename(self):
        options=dict(entity='e',project='p',group='parts',dir=self.temp.name,_sink=self.store,
                     settings={'silent':True,'console':'off'})
        with backprop.Run(id='worker',**options) as worker:
            part=backprop.Artifact('distributed','model')
            with part.new_file('worker') as file:file.write('worker')
            worker.upsert_artifact(part)
        with backprop.Run(id='primary',**options) as primary:
            final=backprop.Artifact('distributed','model')
            with final.new_file('primary') as file:file.write('primary')
            primary.finish_artifact(final)
        collection=self.api.artifact_collection('model','distributed')
        collection.name,collection.type='completed','checkpoint';collection.save()
        self.assertIs(part.wait(timeout=5),part)
        self.assertEqual(part.id,final.id)
        self.assertEqual(part.name,'completed:'+final.version)
        self.assertEqual(part.type,'checkpoint')
        self.assertEqual(set(part.manifest.entries),{'worker','primary'})
