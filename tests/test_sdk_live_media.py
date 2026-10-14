"""Linked run media freezes durably before any upload, including offline retry.

Architecture: real source files and Run journals -> edits/deletion -> controlled
upload faults -> new Journal process view -> exact downloaded bytes and IDs.
Artifacts and Table cells remain eagerly snapshotted; disabled runs do no work.
"""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import backprop
from backprop.media import EncodedMedia as Media
from backprop.transport import Journal, sync, BackpropError


class LiveMediaContracts(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.sink=backprop.MemorySink();self.runs=[]
        self.addCleanup(self.close)
    def close(self):
        for run in self.runs:
            if not run._closed:
                run.finish()
    def run_(self, **settings):
        run=backprop.Run(project='linked',dir=self.root, mode='offline',
            settings={'allow_media_symlink':True,'console':'off','silent':True,'disable_git':True,**settings})
        self.runs.append(run);return run
    def source(self, name='value.bin'):
        path=self.root/name;path.write_bytes(b'before');return path
    def media_events(self):
        return [e for e in self.sink.events if e['type']=='history']
    def descriptor(self,run):
        events=[json.loads(line) for line in Path(run.sync_dir,'events.jsonl').read_text().splitlines()]
        return next(e['payload']['metrics']['media'] for e in events if e['type']=='history')

    def test_source_edits_before_sync_and_repeated_instance_keep_one_binding(self):
        source=self.source();media=Media(source);run=self.run_()
        run.log({'media':media,'nested':[media]});run.log({'media':media});run.finish()
        descriptor=self.descriptor(run)
        self.assertIn('_backprop_live_media',descriptor)
        self.assertEqual(len(list(Path(run.sync_dir,'live-media').glob('*.json'))),1)
        linked=Path(run.dir,'media',descriptor['_backprop_live_media'],source.name)
        self.assertTrue(linked.is_symlink())
        source.write_bytes(b'after');sync(run.sync_dir,sink=self.sink)
        first,second=self.media_events()
        refs=[first['payload']['metrics']['media'],first['payload']['metrics']['nested'][0],second['payload']['metrics']['media']]
        self.assertEqual(len({ref['path'] for ref in refs}),1)
        self.assertEqual(self.sink.download_file(refs[0]),b'after')
        self.assertNotIn('_backprop_live_media',json.dumps(self.sink.events))

    def test_ambiguous_upload_freezes_before_network_then_restart_reuses_bytes(self):
        source=self.source();run=self.run_();run.log({'media':Media(source)});run.finish()
        descriptor=self.descriptor(run);source.write_bytes(b'first upload')
        class FailOnce(backprop.MemorySink):
            def upload_file(inner,path,name):
                result=super().upload_file(path,name)
                if name.endswith('/value.bin'):
                    state=json.loads(Path(run.sync_dir,'live-media',descriptor['_backprop_live_media']+'.snapshot.json').read_text())
                    self.assertIn('_backprop_local_file',state)
                    raise OSError('response lost')
                return result
        failed=FailOnce()
        with self.assertRaisesRegex(BackpropError,'response lost'):sync(run.sync_dir,sink=failed)
        self.assertEqual(Journal(run.sync_dir).acknowledged_count(),0)
        source.unlink()
        sync(run.sync_dir,sink=self.sink)
        self.assertEqual(self.sink.download_file(self.media_events()[0]['payload']['metrics']['media']),b'first upload')
        self.assertEqual(set(failed.files),set(self.sink.files))

    def test_ambiguous_publish_replays_same_ids_and_bytes_after_change(self):
        source=self.source();run=self.run_();run.log({'media':Media(source)});run.finish()
        class Ambiguous(backprop.MemorySink):
            def publish(inner,events):
                super().publish(events);raise OSError('lost publish acknowledgement')
        failed=Ambiguous()
        with self.assertRaises(BackpropError):sync(run.sync_dir,sink=failed)
        source.write_bytes(b'later');sync(run.sync_dir,sink=self.sink)
        self.assertEqual(failed.events,self.sink.events)
        self.assertEqual(self.sink.download_file(self.media_events()[0]['payload']['metrics']['media']),b'before')

    def test_missing_source_before_freeze_errors_without_publication_then_recovers(self):
        source=self.source();run=self.run_();run.log({'media':Media(source)});run.finish();source.unlink()
        with self.assertRaises(BackpropError):sync(run.sync_dir,sink=self.sink)
        self.assertEqual(self.sink.events,[])
        source.write_bytes(b'restored');sync(run.sync_dir,sink=self.sink)
        self.assertEqual(self.sink.download_file(self.media_events()[0]['payload']['metrics']['media']),b'restored')

    def test_false_flag_and_table_cells_keep_original_immutable_bytes(self):
        source=self.source();media=Media(source);run=self.run_(allow_media_symlink=False)
        run.log({'media':media});run.finish();source.write_bytes(b'later');sync(run.sync_dir,sink=self.sink)
        self.assertEqual(self.sink.download_file(self.media_events()[0]['payload']['metrics']['media']),b'before')
        source.write_bytes(b'table original');table_run=self.run_()
        table_run.log({'table':backprop.Table(columns=['media'],data=[[media]])});table_run.finish()
        self.assertFalse(Path(table_run.sync_dir,'live-media').exists())
        source.write_bytes(b'table changed');sync(table_run.sync_dir,sink=self.sink)
        event=next(e for e in self.sink.events if e['run_id']==table_run.id and e['type']=='history')
        self.assertEqual(self.sink.download_file(event['payload']['metrics']['table']['data'][0][0]),b'table original')

    def test_hardlink_and_warned_copy_fallbacks(self):
        for copy_only in [False,True]:
            with self.subTest(copy=copy_only):
                source=self.source('fallback'+str(copy_only));run=self.run_(symlink=False)
                if copy_only:
                    with patch('backprop.live_media.os.link',side_effect=OSError('cross device')),self.assertWarnsRegex(RuntimeWarning,'could not be linked'):
                        run.log({'media':Media(source)})
                else:run.log({'media':Media(source)})
                run.finish();source.write_bytes(b'edited');sync(run.sync_dir,sink=self.sink)
                event=next(e for e in self.sink.events if e['run_id']==run.id and e['type']=='history')
                self.assertEqual(self.sink.download_file(event['payload']['metrics']['media']),b'before' if copy_only else b'edited')

    def test_frozen_snapshot_and_binding_paths_are_validated(self):
        for change in ['identity','snapshot','link']:
            with self.subTest(change=change):
                source=self.source('tamper'+change);run=self.run_();run.log({'media':Media(source)});run.finish()
                descriptor=self.descriptor(run);journal=Journal(run.sync_dir,self.sink)
                state_path=Path(run.sync_dir,'live-media',descriptor['_backprop_live_media']+'.json')
                if change=='snapshot':
                    journal.freeze_live_media(descriptor)
                    state_path=state_path.with_name(descriptor['_backprop_live_media']+'.snapshot.json')
                state=json.loads(state_path.read_text())
                if change=='identity':state['id']='wrong'
                elif change=='snapshot':state['_backprop_local_file']='../../outside'
                else:state['link']='../../outside'
                state_path.write_text(json.dumps(state))
                with self.assertRaises(BackpropError):journal.flush()
                self.assertEqual(self.sink.events,[])

    def test_deadline_and_disabled_runs_do_not_read_or_link_media(self):
        source=self.source();run=self.run_();run.log({'media':Media(source)});run.finish()
        journal=Journal(run.sync_dir,self.sink)
        with patch.object(journal,'freeze_live_media',side_effect=AssertionError('must not read')):
            with self.assertRaises(TimeoutError):journal.flush(deadline=time.monotonic()-1)
        with patch('backprop.live_media.bind',side_effect=AssertionError('must not link')),patch('backprop.transport.Journal.stage_file',side_effect=AssertionError('must not copy')):
            disabled=backprop.Run(dir=self.root/'disabled',mode='disabled',settings={'allow_media_symlink':True})
            disabled.log({'media':Media(source)});disabled.finish()
        self.assertFalse((self.root/'disabled').exists())

    def test_failed_row_does_not_consume_media_and_caption_mime_survive(self):
        source=self.source();media=Media(source,caption='caption',mime_type='application/custom')
        run=self.run_();before=run._journal.count
        with self.assertRaises(TypeError):run.log({'media':media,'invalid':object()})
        self.assertEqual(run._journal.count,before)
        source.write_bytes(b'valid');run.log({'media':media});run.finish();sync(run.sync_dir,sink=self.sink)
        ref=self.media_events()[0]['payload']['metrics']['media']
        self.assertEqual(ref['mime_type'],'application/custom');self.assertEqual(ref['caption'],'caption')
        self.assertEqual(self.sink.download_file(ref),b'valid')

    def test_local_fork_copies_media_snapshot_independent_of_parent(self):
        source=self.source();parent=self.run_();parent.log({'media':Media(source)});parent.finish()
        child=backprop.Run(project='linked',dir=self.root,mode='offline',settings={'fork_from':parent.id+'?_step=0','console':'off','silent':True,'disable_git':True})
        self.runs.append(child);child.finish();source.unlink();sync(child.sync_dir,sink=self.sink)
        event=next(e for e in self.sink.events if e['run_id']==child.id and e['type']=='history')
        self.assertEqual(self.sink.download_file(event['payload']['metrics']['media']),b'before')

    def test_concurrent_journal_freezes_return_the_same_atomic_winner(self):
        import threading
        source=self.source();run=self.run_();run.log({'media':Media(source)});run.finish()
        descriptor=self.descriptor(run);first=Journal(run.sync_dir);second=Journal(run.sync_dir)
        copied=threading.Event();release=threading.Event();results=[];errors=[]
        original=first.stage_file
        def paused(*args,**kwargs):
            value=original(*args,**kwargs);copied.set();release.wait(2);return value
        first.stage_file=paused
        def resolve_first():
            try:results.append(first.freeze_live_media(descriptor))
            except Exception as error:errors.append(error)
        worker=threading.Thread(target=resolve_first);worker.start()
        try:
            self.assertTrue(copied.wait(1));source.write_bytes(b'concurrent winner')
            selected=second.freeze_live_media(descriptor)
        finally:
            release.set();worker.join(2)
        self.assertEqual(errors,[]);self.assertFalse(worker.is_alive());self.assertEqual(results,[selected])
        source.unlink();sync(run.sync_dir,sink=self.sink)
        self.assertEqual(self.sink.download_file(self.media_events()[0]['payload']['metrics']['media']),b'concurrent winner')

    def test_relog_scalar_after_sync_reuses_frozen_bytes_and_artifacts_stay_immutable(self):
        source=self.source('sample.wav');media=backprop.Audio(source);run=self.run_()
        run.log({'media':media});sync(run.sync_dir,sink=self.sink)
        source.write_bytes(b'edited after upload');run.log({'media':media});run.finish();sync(run.sync_dir,sink=self.sink)
        self.assertEqual([self.sink.download_file(e['payload']['metrics']['media']) for e in self.media_events()],[b'before',b'before'])
        artifact=backprop.Artifact('immutable-audio','dataset');artifact.add(media,'audio')
        source.write_bytes(b'changed after Artifact.add')
        artifact_run=self.run_();artifact_run.log_artifact(artifact);artifact_run.finish()
        self.assertFalse(Path(artifact_run.sync_dir,'live-media').exists())
        sync(artifact_run.sync_dir,sink=self.sink)
        api=backprop.Api._from_store(self.sink,project='linked')
        logged=list(api.run(artifact_run.id).logged_artifacts())[0]
        self.assertEqual(logged.get('audio')._bytes(),b'edited after upload')

    def test_batch_rebind_observes_between_log_edits_and_failed_row_keeps_prior_binding(self):
        source=self.source('batch.wav');media=backprop.Audio(source);run=self.run_()
        run.log({'items':[media,media]});sync(run.sync_dir,sink=self.sink)
        source.write_bytes(b'second batch');run.log({'items':[media,media]});sync(run.sync_dir,sink=self.sink)
        source.write_bytes(b'rejected batch')
        count=run._journal.count
        with self.assertRaises(TypeError):run.log({'items':[media,media],'bad':object()})
        self.assertEqual(run._journal.count,count)
        run.log({'media':media});run.finish();sync(run.sync_dir,sink=self.sink)
        events=self.media_events();expected=[b'before',b'second batch']
        for event,data in zip(events[:2],expected):
            items=event['payload']['metrics']['items'];self.assertEqual(items[0],items[1])
            self.assertEqual(self.sink.download_file(items[0]),data)
        self.assertEqual(self.sink.download_file(events[-1]['payload']['metrics']['media']),b'second batch')

    def test_unmodified_pinned_media_binding_reads_new_bytes_for_a_later_batch(self):
        import ast
        import shutil
        from types import SimpleNamespace
        source_root=Path(__file__).resolve().parents[1]/'.reference/wandb/source/wandb/sdk'
        if not source_root.exists():self.skipTest('Pinned source fixture unavailable')
        media_tree=ast.parse((source_root/'data_types/base_types/media.py').read_text())
        filesystem_tree=ast.parse((source_root/'lib/filesystem.py').read_text())
        selected=[node for node in media_tree.body if isinstance(node,(ast.ClassDef,ast.FunctionDef)) and node.name in {'Media','_wb_filename'}]
        selected += [node for node in filesystem_tree.body if isinstance(node,ast.FunctionDef) and node.name=='link_or_copy']
        module=ast.Module(body=[ast.ImportFrom(module='__future__',names=[ast.alias(name='annotations')],level=0),*selected],type_ignores=[])
        namespace={'WBValue':object,'os':os,'hashlib':hashlib,'shutil':shutil,'pathlib':__import__('pathlib'),
                   'util':SimpleNamespace(make_file_path_upload_safe=lambda key:key),'are_paths_on_same_drive':lambda *args:True}
        exec(compile(ast.fix_missing_locations(module),'<pinned unchanged Media/link_or_copy>','exec'),namespace)
        filesystem=SimpleNamespace(link_or_copy=namespace['link_or_copy'],mkdir_exists_ok=lambda path:Path(path).mkdir(parents=True,exist_ok=True))
        namespace['filesystem']=filesystem
        cls=namespace['Media'];cls.get_media_subdir=classmethod(lambda cls:'media')
        source=self.source('pinned.wav');media=cls();media._set_file(str(source))
        directory=self.root/'pinned-run';directory.mkdir()
        run=SimpleNamespace(dir=str(directory),_settings=SimpleNamespace(allow_media_symlink=True,symlink=True),_publish_file=lambda _:None)
        media.bind_to_run(run,'items',0);first=Path(media._path).read_bytes()
        source.write_bytes(b'after first upload')
        media.bind_to_run(run,'items',1);second=Path(media._path).read_bytes()
        self.assertEqual((first,second),(b'before',b'after first upload'))

    def test_same_row_and_nested_scalar_aliases_see_preceding_batch(self):
        for nested in [False,True]:
            with self.subTest(nested=nested):
                source=self.source('same-row'+str(nested)+'.wav');media=backprop.Audio(source);run=self.run_()
                run.log({'media':media});sync(run.sync_dir,sink=self.sink)
                source.write_bytes(b'new row')
                values={'batch':[media],'scalar':media}
                run.log({'nested':values} if nested else values);run.finish();sync(run.sync_dir,sink=self.sink)
                event=[e for e in self.media_events() if e['run_id']==run.id][-1]
                result=event['payload']['metrics'];result=result['nested'] if nested else result
                self.assertEqual(result['batch'][0],result['scalar'])
                self.assertEqual(self.sink.download_file(result['scalar']),b'new row')

    def test_pending_batch_alias_survives_replacement_only_when_still_referenced(self):
        for preserve_alias in [False,True]:
            with self.subTest(preserve_alias=preserve_alias):
                source=self.source('pending'+str(preserve_alias)+'.wav');media=backprop.Audio(source);run=self.run_()
                run.log({'media':media});sync(run.sync_dir,sink=self.sink)
                source.write_bytes(b'pending binding');run.log({'batch':[media]},commit=False)
                if preserve_alias:run.log({'alias':media},commit=False)
                run.log({'batch':0,'scalar':media});run.finish();sync(run.sync_dir,sink=self.sink)
                event=[e for e in self.media_events() if e['run_id']==run.id][-1]
                expected=b'pending binding' if preserve_alias else b'before'
                self.assertEqual(self.sink.download_file(event['payload']['metrics']['scalar']),expected)
                if preserve_alias:self.assertEqual(event['payload']['metrics']['alias'],event['payload']['metrics']['scalar'])

    def test_pending_batch_then_scalar_commits_and_failed_new_overlay_is_discarded(self):
        source=self.source('overlay.wav');media=backprop.Audio(source);run=self.run_()
        run.log({'media':media});sync(run.sync_dir,sink=self.sink)
        source.write_bytes(b'pending');run.log({'batch':[media]},commit=False)
        original_binding=run._pending['batch'][0]['_backprop_live_media']
        with self.assertRaises(TypeError):run.log({'replacement':[media],'bad':object()})
        run.log({'scalar':media});run.finish();sync(run.sync_dir,sink=self.sink)
        result=self.media_events()[-1]['payload']['metrics']
        self.assertEqual(result['batch'][0],result['scalar'])
        events=[json.loads(line) for line in Path(run.sync_dir,'events.jsonl').read_text().splitlines()]
        raw=[event for event in events if event['type']=='history'][-1]['payload']['metrics']
        self.assertEqual(raw['scalar']['_backprop_live_media'],original_binding)

    def test_latest_prepared_binding_wins_even_when_pending_key_order_differs(self):
        source=self.source('order.wav');media=backprop.Audio(source);run=self.run_()
        run.log({'first':[media]},commit=False);run.log({'second':[media]},commit=False)
        run.log({'first':[media]},commit=False)
        latest=run._pending['first'][0]['_backprop_live_media']
        run.log({'scalar':media});run.finish()
        raw=[json.loads(line) for line in Path(run.sync_dir,'events.jsonl').read_text().splitlines()]
        event=next(e for e in raw if e['type']=='history')
        self.assertEqual(event['payload']['metrics']['scalar']['_backprop_live_media'],latest)
        self.assertEqual(run._journal.stage_live_media(media)['_backprop_live_media'],latest)

    def test_explicit_step_advance_keeps_binding_from_the_committed_previous_row(self):
        source=self.source('step.wav');media=backprop.Audio(source);run=self.run_()
        run.log({'media':media});sync(run.sync_dir,sink=self.sink)
        source.write_bytes(b'next row');run.log({'value':[media]},step=1)
        binding=run._pending['value'][0]['_backprop_live_media']
        run.log({'value':media},step=2,commit=True);run.finish()
        records=[json.loads(line) for line in Path(run.sync_dir,'events.jsonl').read_text().splitlines()]
        last=[event for event in records if event['type']=='history'][-1]
        self.assertEqual(last['payload']['metrics']['value']['_backprop_live_media'],binding)

if __name__=='__main__':unittest.main()
