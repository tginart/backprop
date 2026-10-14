"""Verify group encoding and read-time distributed output relationships.

Architecture: genuine pinned public methods/encoder -> real multi-run journals
-> complete output views, aliases and immutable file checks. Process-exit/late
generation fixtures are shared with the browser. Main caller: SDK release CI.
"""
import copy,importlib.metadata,json,os,subprocess,sys,tempfile,unittest
from pathlib import Path
from runtime_test_case import ContextTestCase
from unittest.mock import patch
import backprop as w
from backprop.artifact_events import iter_artifact_events
from test_artifact_identity import SETTINGS
ROOT=Path(__file__).resolve().parents[1]


class DistributedInputsLineage(ContextTestCase):
    def setUp(self):
        self.directory=Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.dict(os.environ,{'BACKPROP_DIR':str(self.directory)}))
        self.sink=w.MemorySink()

    def run_handle(self,name,**kwargs):
        run=w.init(id=name,entity='e',project='p',group='default',dir=str(self.directory),
            _sink=self.sink,_deployment={},settings=SETTINGS,reinit='create_new',**kwargs)
        self.addCleanup(lambda:run.finish() if not run._closed else None)
        return run

    def part(self,name):
        artifact=w.Artifact('model','model')
        with artifact.new_file(name) as out:out.write(name)
        return artifact

    def test_pinned_group_values_errors_side_effects_and_aliases(self):
        try:reference=importlib.metadata.distribution('wandb')
        except importlib.metadata.PackageNotFoundError:self.skipTest('Pinned W&B unavailable')
        self.assertEqual(reference.version,'0.30.0');evidence={}
        for module in ['wandb','wandb.sdk.wandb_run']:
            observations=[]
            for mode in ['reference','native']:
                child=subprocess.run([sys.executable,'-B',str(ROOT/'tests/distributed_group_values_probe.py'),mode,module],
                    capture_output=True,text=True,cwd=tempfile.gettempdir(),timeout=40,
                    env={**os.environ,'PYTHONPATH':str(reference.locate_file('')) if mode=='reference' else str(ROOT/'sdk')})
                self.assertEqual(child.returncode,0,child.stderr)
                result=json.loads(child.stdout);result.pop('source_sha256',None);observations.append(result)
            self.assertEqual(len(observations[0]),30);self.assertEqual(*observations);evidence[module]=observations
        if os.environ.get('CI'):
            folder=ROOT/'test-results';folder.mkdir(exist_ok=True)
            (folder/'distributed-artifact-group-values.json').write_text(json.dumps(evidence))

    def test_encoded_groups_share_a_union_without_changing_local_descriptors(self):
        for index,(first_group,last_group) in enumerate([(b'\xe9\x9b\xaa','雪'),([],False),(0,{}),(b'',0.0)]):
            first=self.run_handle('first'+str(index));primary=self.run_handle('primary'+str(index))
            a=first.upsert_artifact(self.part('first'),distributed_id=first_group)
            b=primary.finish_artifact(self.part('primary'),distributed_id=last_group)
            self.assertIs(a.distributed_id,first_group);self.assertIs(b.distributed_id,last_group)
            self.assertIs(a.wait(),a);self.assertEqual(a.id,b.id)
            self.assertEqual(set(a.manifest.entries),{'first','primary'})
            self.assertIs(a.distributed_id,first_group)
            with self.assertRaises(TypeError):first.upsert_artifact(self.part('invalid'),distributed_id=True)

    def test_independent_outputs_edits_relogging_and_no_reader_writes(self):
        worker=self.run_handle('worker');a=worker.upsert_artifact(self.part('worker'));worker.finish()
        primary=self.run_handle('primary');primary.finish_artifact(self.part('primary'),aliases=['best']);primary.finish()
        source=Path(worker.sync_dir,'events.jsonl').read_bytes();before=copy.deepcopy(self.sink.events)
        api=w.Api._from_store(self.sink,entity='e',project='p')
        for name in ['worker','primary']:
            values=list(api.run(name).logged_artifacts());self.assertEqual(len(values),1)
            self.assertEqual(set(values[0].manifest.entries),{'worker','primary'})
        self.assertEqual(self.sink.events,before);self.assertEqual(Path(worker.sync_dir,'events.jsonl').read_bytes(),source)
        a.wait();a.metadata={'checked':True};a.aliases=['chosen'];a.save()
        for name in ['worker','primary']:
            artifact=list(api.run(name).logged_artifacts())[0]
            self.assertEqual(artifact.metadata,{'checked':True});self.assertEqual(artifact.aliases,['chosen'])
        with self.run_handle('reader') as reader:reader.log_artifact(a)
        self.assertEqual(len(api.run('reader').logged_artifacts()),1)
        expanded=list(iter_artifact_events(self.sink.events))
        self.assertEqual(list(iter_artifact_events(expanded)),expanded)

    def test_scope_and_incomplete_membership_are_not_guessed(self):
        worker=self.run_handle('worker');worker.upsert_artifact(self.part('worker'))
        primary=self.run_handle('primary');primary.finish_artifact(self.part('primary'))
        source=next(e for e in self.sink.events if e['type']=='artifact_part' and e['run_id']=='worker')
        variants=[]
        for field in ['entity','project','name','artifact_type','distributed_id','part_id']:
            part=copy.deepcopy(source);part['event_id']='unrelated-'+field;part['run_id']='unrelated-'+field
            if field in {'entity','project'}:part[field]='other'
            elif field in {'name','artifact_type'}:part['payload']['manifest'][field]='other'
            else:part['payload'][field]='other'
            variants.append(part)
        records=list(iter_artifact_events([*self.sink.events,*variants]))
        self.assertFalse(any(e['run_id'].startswith('unrelated-') for e in records))

    def test_conflicting_completion_membership_and_files_raise(self):
        worker=self.run_handle('worker');worker.upsert_artifact(self.part('worker'))
        primary=self.run_handle('primary');primary.finish_artifact(self.part('primary'))
        final=next(e for e in self.sink.events if e['type']=='artifact')
        other=copy.deepcopy(final);other['event_id']='conflicting-final';other['payload']['artifact_id']='other'
        with self.assertRaisesRegex(ValueError,'conflicting completed artifacts'):
            list(iter_artifact_events([*self.sink.events,other]))
        rows=copy.deepcopy(self.sink.events)
        for event in rows:
            if event['type']=='artifact':event['payload']['files']=[r for r in event['payload']['files'] if r['name']!='worker']
        with self.assertRaisesRegex(ValueError,'original contribution file'):list(iter_artifact_events(rows))
        for field,message in [('contribution_ids','invalid contribution IDs'),('distributed_id','invalid distributed group')]:
            rows=copy.deepcopy(self.sink.events)
            for event in rows:
                if event['type']=='artifact':event['payload'][field]=None
            with self.assertRaisesRegex(ValueError,message):list(iter_artifact_events(rows))

    def test_provider_descriptors_with_equal_etags_do_not_hide_different_references(self):
        from backprop.reference_handlers.runtime import HTTPSession
        from test_sdk_external_references import Response
        for conflict in [False,True]:
            group='external-'+str(conflict)
            worker=self.run_handle('worker-'+group);primary=self.run_handle('primary-'+group)
            def artifact(uri):
                value=w.Artifact('external','dataset')
                with patch.object(HTTPSession,'get',return_value=Response(uri)):
                    value.add_reference(uri,name='data')
                return value
            uri='https://example.invalid/a'
            first=artifact(uri);last=artifact('https://example.invalid/b' if conflict else uri)
            with patch.object(worker._journal,'stage_file',side_effect=AssertionError('External bytes uploaded')):
                worker.upsert_artifact(first,distributed_id=group)
            with patch.object(primary._journal,'stage_file',side_effect=AssertionError('External bytes uploaded')):
                if conflict:
                    with self.assertRaisesRegex(ValueError,'Conflicting distributed artifact path'):
                        primary.finish_artifact(last,distributed_id=group)
                    self.assertFalse(any(e['type']=='artifact' and e['run_id']==primary.id for e in self.sink.events))
                else:
                    primary.finish_artifact(last,distributed_id=group)
                    first.wait()
                    self.assertEqual(first.get_entry('data').ref,uri)
                    loaded=w.Api._from_store(self.sink,entity='e',project='p').run(worker.id).logged_artifacts()[0]
                    self.assertEqual(loaded.get_entry('data').ref,uri)

    def test_repeat_calls_validate_arguments_and_publish_new_descriptive_intent(self):
        self.compare_reference('distributed_repeat_calls_probe.py',72)

    def compare_reference(self,filename,count):
        """Retain source-backed observations for repeated calls in CI.

        Main caller: repeat-call regression. Artifact state and network merge
        behavior have separate real journal tests; this comparison exercises
        actual public preparation and the reference's transport field encoding.
        """
        try:reference=importlib.metadata.distribution('wandb')
        except importlib.metadata.PackageNotFoundError:self.skipTest('Pinned W&B unavailable')
        self.assertEqual(reference.version,'0.30.0');observations=[]
        for mode in ['reference','native']:
            child=subprocess.run([sys.executable,'-B',str(ROOT/'tests'/filename),mode],
                capture_output=True,text=True,cwd=tempfile.gettempdir(),timeout=60,
                env={**os.environ,'PYTHONHASHSEED':'1' if mode=='reference' else '2',
                    'PYTHONPATH':str(reference.locate_file('')) if mode=='reference' else str(ROOT/'sdk')})
            self.assertEqual(child.returncode,0,child.stderr);observations.append(json.loads(child.stdout))
            if os.environ.get('CI'):
                folder=ROOT/'test-results';folder.mkdir(exist_ok=True)
                (folder/f'distributed-artifact-repeat-{mode}.json').write_text(child.stdout)
        self.assertEqual(len(observations[0])-1,count)
        for key,expected in observations[0].items():
            if key!='source_hashes':self.assertEqual(observations[1][key],expected,key)

    def test_failed_final_call_retries_original_but_changed_intent_creates_new_round(self):
        for changed in [False,True]:
            run=self.run_handle('retry-'+str(changed));a=self.part('own')
            publish=self.sink.publish
            def lost(events):
                publish(events)
                if any(e['type']=='artifact' for e in events):raise OSError('lost response')
            with patch.object(self.sink,'publish',side_effect=lost):
                with self.assertRaises(w.Error):run.finish_artifact(a,aliases=['first'])
            original=next(e for e in self.sink.events if e['type']=='artifact' and e['run_id']==run.id)
            with self.assertRaises(ValueError):run.finish_artifact(a,aliases=['bad/name'])
            aliases=['second'] if changed else ['first']
            if changed:a.metadata={'edited':True}
            self.assertIs(run.finish_artifact(a,aliases=aliases),a)
            outputs={e['event_id']:e for e in self.sink.events if e['type']=='artifact' and e['run_id']==run.id}
            self.assertIn(original['event_id'],outputs);self.assertEqual(len(outputs),2 if changed else 1)
            view=list(w.Api._from_store(self.sink,entity='e',project='p').run(run.id).logged_artifacts())
            self.assertEqual(len(view),len(outputs))
            if changed:
                edited=next(e for e in outputs.values() if e['event_id']!=original['event_id'])
                self.assertEqual(edited['payload']['metadata'],{'edited':True})
                self.assertIn('second',edited['payload']['aliases'])
                self.assertTrue(set(original['payload']['contribution_ids']).isdisjoint(edited['payload']['contribution_ids']))

    def test_offline_repeated_final_calls_have_distinct_durable_generations(self):
        run=w.init(id='offline-repeat',entity='e',project='p',group='offline',dir=str(self.directory),
            mode='offline',_deployment={},settings=SETTINGS,reinit='create_new')
        a=self.part('own');run.finish_artifact(a,aliases=['first'])
        a.metadata={'round':2};run.finish_artifact(a,aliases=['second']);run.finish()
        before=Path(run.sync_dir,'events.jsonl').read_bytes()
        w.sync(run.sync_dir,sink=self.sink);w.sync(run.sync_dir,sink=self.sink)
        self.assertEqual(Path(run.sync_dir,'events.jsonl').read_bytes(),before)
        finals=[e['payload'] for e in self.sink.events if e['type']=='artifact']
        self.assertEqual(len(finals),2)
        self.assertNotEqual(finals[0]['artifact_id'],finals[1]['artifact_id'])
        self.assertTrue(set(finals[0]['contribution_ids']).isdisjoint(finals[1]['contribution_ids']))
        latest=w.Api._from_store(self.sink,entity='e',project='p').artifact('model:latest')
        self.assertEqual(latest.metadata,{'round':2});self.assertIn('second',latest.aliases)

    def test_failed_committed_source_can_continue_in_another_group_after_recovery(self):
        run=self.run_handle('source');a=run.log_artifact(self.part('own'));run.flush()
        identity=a.id;publish=self.sink.publish
        def lost(events):
            publish(events)
            if any(e['type']=='artifact_part' for e in events):raise OSError('lost part response')
        with patch.object(self.sink,'publish',side_effect=lost):
            with self.assertRaises(w.Error):run.upsert_artifact(a,distributed_id='first')
        self.assertIs(run.upsert_artifact(a,distributed_id='second'),a)
        self.assertEqual(a.id,identity)
        self.assertEqual({e['payload']['distributed_id'] for e in self.sink.events if e['type']=='artifact_part'},{'first','second'})


class DistributedProviderContracts(ContextTestCase):
    def compare(self,filename,count):
        """Run the genuine provider handlers through both distributed entry paths.

        Main callers: cloud and HTTP comparisons below. Only upstream delivery
        is replaced; publication preparation, reference encoding and errors run.
        Optional requests and pinned W&B are supplied by the release CI runtime.
        """
        try:reference=importlib.metadata.distribution('wandb');import requests
        except (importlib.metadata.PackageNotFoundError,ImportError):self.skipTest('Pinned W&B and requests unavailable')
        self.assertEqual(reference.version,'0.30.0');observations=[]
        for mode in ['reference','native']:
            child=subprocess.run([sys.executable,'-B',str(ROOT/'tests'/filename),mode,'distributed'],
                capture_output=True,text=True,cwd=tempfile.gettempdir(),timeout=180,
                env={**os.environ,'PYTHONPATH':str(reference.locate_file('')) if mode=='reference' else str(ROOT/'sdk')})
            self.assertEqual(child.returncode,0,child.stderr);observations.append(json.loads(child.stdout))
            if os.environ.get('CI'):
                folder=ROOT/'test-results';folder.mkdir(exist_ok=True)
                (folder/f'distributed-artifact-{Path(filename).stem}-{mode}.json').write_text(child.stdout)
        self.assertEqual(len(observations[0])-1,count)
        for key,expected in observations[0].items():
            if key!='source_hashes':self.assertEqual(observations[1][key],expected,key)

    def test_cloud_success_errors_and_validation_order(self):
        self.compare('artifact_provider_reference_probe.py',152)

    def test_real_http_retries_and_provider_errors(self):
        self.compare('artifact_http_reference_probe.py',48)
