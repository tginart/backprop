"""Verify public provider publication and real HTTP error/retry behavior.

Architecture: isolated pinned cloud comparison -> loopback HTTP server -> real
public logging and downloads -> durable error/retry observations. Main callers:
SDK and CI tests. Cloud services are explicit fixtures; HTTP uses real requests.
"""
import importlib.metadata,importlib.util,json,os,subprocess,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]


class ProviderPublicationContracts(unittest.TestCase):
    def test_real_public_entry_points_propagate_cloud_errors_before_publication(self):
        try:reference=importlib.metadata.distribution('wandb')
        except importlib.metadata.PackageNotFoundError:self.skipTest('Pinned W&B unavailable')
        self.assertEqual(reference.version,'0.30.0');observations=[]
        for mode in ['reference','native']:
            child=subprocess.run([sys.executable,'-B',str(ROOT/'tests/artifact_provider_reference_probe.py'),mode],cwd=tempfile.gettempdir(),text=True,capture_output=True,timeout=180,
                env={**os.environ,'PYTHONPATH':str(reference.locate_file('')) if mode=='reference' else str(ROOT/'sdk')})
            self.assertEqual(child.returncode,0,child.stderr);observations.append(json.loads(child.stdout))
            if os.environ.get('CI'):
                directory=ROOT/'test-results';directory.mkdir(exist_ok=True);(directory/f'artifact-provider-{mode}.json').write_text(child.stdout)
        self.assertEqual(len(observations[0])-1,228)
        for key,expected in observations[0].items():
            if key!='source_hashes':self.assertEqual(observations[1][key],expected,key)


    def test_genuine_http_success_errors_and_transient_retries_match_pinned(self):
        try:reference=importlib.metadata.distribution('wandb');import requests
        except (importlib.metadata.PackageNotFoundError,ImportError):self.skipTest('Pinned W&B and requests unavailable')
        observations=[]
        for mode in ['reference','native']:
            child=subprocess.run([sys.executable,'-B',str(ROOT/'tests/artifact_http_reference_probe.py'),mode],cwd=tempfile.gettempdir(),text=True,capture_output=True,timeout=120,
                env={**os.environ,'PYTHONPATH':str(reference.locate_file('')) if mode=='reference' else str(ROOT/'sdk')})
            self.assertEqual(child.returncode,0,child.stderr);observations.append(json.loads(child.stdout))
            if os.environ.get('CI'):
                directory=ROOT/'test-results';directory.mkdir(exist_ok=True);(directory/f'artifact-http-{mode}.json').write_text(child.stdout)
        self.assertEqual(len(observations[0])-1,72)
        for key,expected in observations[0].items():
            if key=='source_hashes':continue
            self.assertEqual(observations[1][key],expected,key)
            if key.endswith('/transient'):self.assertEqual(expected['requests'],2)
            if key.endswith('/exhausted'):
                self.assertEqual(expected['requests'],17);self.assertEqual(expected['error'],'RetryError')
            if key.endswith(('/missing','/denied')):self.assertEqual(expected['error'],'HTTPError')

    @unittest.skipUnless(importlib.util.find_spec("requests"), "Optional requests runtime unavailable")
    def test_http_session_is_lazy_closes_failed_response_and_matches_retry_configuration(self):
        import requests
        from backprop.reference_handlers.runtime import HTTPSession
        session=HTTPSession();self.assertIsNone(session._session);session.close()
        response=requests.Response();response.status_code=403;response.url='https://example.invalid/file'
        response.request=requests.Request('GET',response.url).prepare();response._content=b'denied';response._content_consumed=True
        with patch.object(requests.Session,'get',return_value=response),patch.object(response,'close') as close:
            with self.assertRaises(requests.HTTPError):session.get(response.url)
            close.assert_called_once()
        provider=session._session;retry=provider.adapters['https://'].max_retries
        self.assertEqual(retry.total,16);self.assertEqual(retry.backoff_factor,1)
        self.assertEqual(tuple(retry.status_forcelist),(308,408,409,429,500,502,503,504))
        with patch.object(provider,'close',wraps=provider.close) as close:
            session.close();close.assert_called_once();self.assertIsNone(session._session)

    @unittest.skipUnless(importlib.util.find_spec("requests"), "Optional requests runtime unavailable")
    def test_failed_reference_download_keeps_existing_destination_and_can_retry(self):
        import threading
        from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
        import requests,backprop as w
        from test_artifact_identity import SETTINGS
        state={'status':200}
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(state['status']);self.send_header('ETag','"v1"');self.send_header('Content-Length','7');self.end_headers();self.wfile.write(b'content')
            def log_message(self,*args):pass
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            with tempfile.TemporaryDirectory() as temp,patch.dict(os.environ,{'BACKPROP_DIR':temp}):
                sink=w.MemorySink()
                with w.init(id='writer',entity='e',project='p',dir=temp,_sink=sink,_deployment={},settings=SETTINGS) as run:
                    artifact=run.log_artifact(f'http://127.0.0.1:{server.server_port}/file',name='http-model');run.flush()
                    loaded=w.Api._from_store(sink,entity='e',project='p').artifact(artifact.id)
                    destination=Path(temp)/'download';destination.mkdir();(destination/'file').write_bytes(b'previous')
                    state['status']=403
                    with self.assertRaises(requests.HTTPError):loaded.get_entry('file').download(destination)
                    self.assertEqual((destination/'file').read_bytes(),b'previous')
                    state['status']=200
                    self.assertEqual(Path(loaded.get_entry('file').download(destination)).read_bytes(),b'content')
        finally:server.shutdown();server.server_close();thread.join()
