"""Pinned Colab/Kaggle interfaces through actual SDK artifacts, without services.

Architecture: fake only the hosted provider boundary -> real discovery/capture
and immutable storage -> inspect exported bytes. These tests prove adapter
contracts, not availability of either live hosted notebook service.
"""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import warnings

import backprop
from backprop.notebook_discovery import resolve_notebook
from backprop.notebook_providers import export_notebook, export_name
from backprop.notebook_snapshot import NotebookRedactor, CaptureLimitError


def notebook(source='print(1)'):
    return {'nbformat':4,'nbformat_minor':5,'metadata':{},'cells':[
        {'cell_type':'code','execution_count':1,'metadata':{},'source':source,'outputs':[]}]}


class Events:
    def __init__(self): self.callbacks=[]
    def register(self, name, callback): self.callbacks.append(callback)
    def unregister(self, name, callback): self.callbacks.remove(callback)


class NotebookProviderTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.addCleanup(backprop.teardown)
        self.addCleanup(patch.stopall)
        self.shell=SimpleNamespace(kernel=SimpleNamespace(shell=SimpleNamespace(user_ns={})),config={},
            events=Events(),display_pub=SimpleNamespace(publish=lambda *args,**kwargs:None),execution_count=1)
        self.sink=backprop.MemorySink()
        patch.dict(sys.modules,{'IPython':SimpleNamespace(get_ipython=lambda:self.shell),
            'google.colab':None,'kaggle_session':None}).start()
        self.location_patcher=patch('backprop.notebook_discovery._from_servers',return_value=None)
        self.location=self.location_patcher.start()
        patch.dict(os.environ,{'BACKPROP_TOKEN':'private-provider-token'}).start()
        self.value=notebook("api_key='private-provider-token'\nprint(1)")
        self.calls=[]

    def colab(self):
        def blocking_request(name,timeout_sec):
            self.calls.append((name,timeout_sec,threading.current_thread()))
            return {'ipynb':copy.deepcopy(self.value)}
        module=SimpleNamespace(_message=SimpleNamespace(blocking_request=blocking_request))
        patch.dict(sys.modules,{'google.colab':module}).start()
        return module

    def kaggle(self, export=None):
        def request():
            self.calls.append(threading.current_thread())
            return {'source':json.dumps(self.value)}
        module=SimpleNamespace(UserSessionClient=lambda:SimpleNamespace(get_exportable_ipynb=export or request))
        patch.dict(sys.modules,{'kaggle_session':module}).start()
        return module

    def new(self, **settings):
        return backprop.init(project='provider-test',dir=self.temp.name,save_code=True,_sink=self.sink,_deployment={},
            settings={'console':'off','silent':True,'disable_git':True,**settings})

    def artifacts(self):
        return [event['payload'] for event in self.sink.events if event['type']=='artifact']

    def test_colab_export_init_cell_finish_and_fileid_url_on_main_thread(self):
        self.colab();self.location.return_value=('fileId=id%2Fone','Training')
        run=self.new()
        self.assertEqual(run.settings.colab_url,'https://colab.research.google.com/notebook#fileId=id/one')
        self.assertEqual(self.calls[0][0],'get_ipynb')
        self.assertTrue(0<self.calls[0][1]<=5)
        self.assertIs(self.calls[0][2],threading.main_thread())
        self.value=notebook('print(2)')
        self.shell.events.callbacks[0]()
        run.flush()
        self.value=notebook('print(3)')
        run.finish()
        artifacts=self.artifacts()
        self.assertEqual(len(artifacts),3)
        sources=[json.loads(self.sink.download_file(artifact['files'][0]))['cells'][0]['source'] for artifact in artifacts]
        self.assertEqual(sources[1:],['print(2)','print(3)'])
        self.assertIn('[redacted]',sources[0]);self.assertNotIn('private-provider-token',sources[0])
        self.assertEqual(artifacts[0]['files'][0]['name'],'Training.ipynb')
        self.assertEqual(self.shell.events.callbacks,[])
        metadata=json.loads((Path(run.dir)/'wandb-metadata.json').read_text())
        self.assertEqual(metadata['colab'],run.settings.colab_url)
        self.assertIsNone(metadata.get('codePathLocal'))

    def test_colab_fallback_filename_and_safe_provider_name(self):
        self.colab();run=self.new();run.finish()
        self.assertEqual(self.artifacts()[0]['files'][0]['name'],'colab.ipynb')
        self.assertEqual(export_name('../../nested/Notebook','colab.ipynb'),'Notebook.ipynb')
        self.assertIsNone(run.settings.colab_url)

    def test_kaggle_exact_client_contract_redaction_and_nonempty_rule(self):
        self.kaggle();run=self.new();run.finish()
        artifact=self.artifacts()[0]
        value=json.loads(self.sink.download_file(artifact['files'][0]))
        self.assertEqual(value['metadata']['name'],'kaggle.ipynb')
        self.assertEqual(artifact['files'][0]['name'],'kaggle.ipynb')
        self.assertNotIn('private-provider-token',json.dumps(value))
        self.assertIsNot(self.calls[0],threading.main_thread())
        settings=backprop.Settings();settings._notebook_provider='kaggle'
        self.value['cells']=[]
        self.assertIsNone(export_notebook(settings,NotebookRedactor(run),time.monotonic()+1))

    def test_empty_colab_export_uses_available_kaggle_as_pinned(self):
        module=self.colab();self.kaggle()
        module._message.blocking_request=lambda *args,**kwargs:None
        run=self.new();run.finish()
        self.assertEqual(self.artifacts()[0]['files'][0]['name'],'kaggle.ipynb')

    def test_saved_file_wins_without_provider_export(self):
        self.colab()
        path=Path(self.temp.name)/'saved.ipynb';path.write_text(json.dumps(notebook('local')))
        run=self.new(notebook_name=str(path));run.finish()
        self.assertFalse(self.calls)
        self.assertEqual(json.loads(self.sink.download_file(self.artifacts()[0]['files'][0]))['cells'][0]['source'],'local')

    def test_disabled_and_save_code_false_never_call_export(self):
        self.colab()
        for settings in ({'disable_code':True},{'mode':'disabled'},{'save_code':False}):
            run=backprop.init(project='provider-test',dir=self.temp.name,_sink=self.sink,_deployment={},
                settings={'console':'off','silent':True,'label_disable':True,**settings})
            run.finish()
        self.assertFalse(self.calls)

    def test_colab_is_never_called_from_worker_thread(self):
        self.colab();run=self.new();before=len(self.calls)
        errors=[]
        def worker():
            try: export_notebook(run._settings,NotebookRedactor(run),time.monotonic()+1)
            except Exception as error: errors.append(error)
        thread=threading.Thread(target=worker);thread.start();thread.join(1)
        self.assertIsInstance(errors[0],RuntimeError)
        self.assertEqual(len(self.calls),before)
        run.finish()

    def test_colab_late_response_is_not_persisted(self):
        module=self.colab();run=self.new();before=run._journal.count
        def late(*args,**kwargs):
            time.sleep(.03);return {'ipynb':notebook('late')}
        module._message.blocking_request=late
        self.assertFalse(run._notebook.refresh(deadline=time.monotonic()+.01))
        self.assertEqual(run._journal.count,before)
        run._notebook.enabled=False;run.finish()

    def test_kaggle_timeout_has_no_late_artifact_or_settings_mutation(self):
        release,done=threading.Event(),threading.Event()
        self.kaggle();run=self.new();before=run._journal.count
        def blocked():
            release.wait(2);done.set();return {'source':json.dumps(notebook('late'))}
        sys.modules['kaggle_session'].UserSessionClient=lambda:SimpleNamespace(get_exportable_ipynb=blocked)
        self.assertFalse(run._notebook.refresh(deadline=time.monotonic()+.02))
        self.assertEqual(run._journal.count,before)
        release.set();self.assertTrue(done.wait(1))
        self.assertEqual(run._journal.count,before)
        run._notebook.enabled=False;run.finish()

    def test_provider_failure_is_safe_and_does_not_drop_session_history(self):
        module=self.colab();run=self.new()
        module._message.blocking_request=lambda *args,**kwargs:(_ for _ in ()).throw(ValueError('private-provider-token'))
        self.shell.history_manager=SimpleNamespace(get_range=lambda output:[(1,1,('print(1)','1')),(1,2,('print(2)','2'))])
        run.finish()
        names=[file['name'] for artifact in self.artifacts() for file in artifact['files']]
        self.assertIn('_session_history.ipynb',names)
        self.assertFalse(any('private-provider-token' in value for value in run._warnings))

    def test_malformed_or_oversize_provider_response_is_rejected(self):
        self.kaggle();run=self.new();settings=run._settings;redactor=NotebookRedactor(run)
        for value in ({'cells':[]}, ['wrong']):
            self.value=value
            with self.assertRaises(ValueError): export_notebook(settings,redactor,time.monotonic()+1)
        self.value=notebook('long'*100)
        with patch('backprop.notebook_providers.MAX_NOTEBOOK_BYTES',100):
            with self.assertRaises(CaptureLimitError): export_notebook(settings,redactor,time.monotonic()+1)
        run._notebook.enabled=False;run.finish()

    def test_colab_virtual_jupyter_metadata_and_url_only_when_provider_loaded(self):
        self.colab()
        self.location_patcher.stop()
        from backprop.notebook_discovery import _from_servers
        with patch('backprop.notebook_discovery._kernel_and_servers',return_value=('active',[{'url':'http://local.invalid/'}])), \
             patch('backprop.notebook_discovery._query_sessions',return_value=[{'kernel':{'id':'active'},'type':'notebook','path':'fileId=abc%2Fdef','name':'My Notebook'}]):
            self.assertEqual(_from_servers(time.monotonic()+1,provider='colab'),('fileId=abc%2Fdef','My Notebook'))
        settings=backprop.Settings();settings._notebook_jupyter_path='fileId=abc%2Fdef'
        self.assertEqual(settings.colab_url,'https://colab.research.google.com/notebook#fileId=abc/def')
        with patch.dict(sys.modules,{'google.colab':None}):
            # Pinned _colab checks module membership, not module truthiness.
            self.assertEqual(settings.colab_url,'https://colab.research.google.com/notebook#fileId=abc/def')
        settings._notebook_jupyter_path='saved.ipynb'
        self.assertIsNone(settings.colab_url)


if __name__=='__main__': unittest.main()
