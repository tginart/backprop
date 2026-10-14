"""Compare HTTP publication through real local servers and HTTP clients.

Architecture: loopback response sequence -> genuine pinned/native public call
-> actual provider session/handler -> error or captured manifest. Main caller:
provider publication tests. Only W&B delivery and collection lookup are fixtures.
"""
import hashlib,importlib,json,os,sys,tempfile,threading
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];REFERENCE=sys.argv[1]=='reference'
DISTRIBUTED=len(sys.argv)>2 and sys.argv[2]=='distributed'
if REFERENCE:import wandb as w
else:
    import backprop as w
    from backprop.compatibility import install_wandb_aliases
    install_wandb_aliases()


def build():
    out={};requests={};responses={}
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests[self.path]=requests.get(self.path,0)+1
            sequence=responses[self.path];status=sequence[min(requests[self.path]-1,len(sequence)-1)]
            self.send_response(status);self.send_header('Content-Length','7');self.send_header('ETag','"v1"');self.end_headers();self.wfile.write(b'content')
        def log_message(self,*args):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    base=f'http://127.0.0.1:{server.server_port}'
    try:
      for entry in (['wandb.Run','wandb.sdk.wandb_run.Run'] if DISTRIBUTED else ['wandb','wandb.Run','wandb.sdk.wandb_run.Run']):
       for operation in (['upsert_artifact','finish_artifact'] if DISTRIBUTED else ['log_artifact','log_model']):
        for mode in ['online','offline']:
         for case,sequence in [('success',[200]),('denied',[403]),('missing',[404]),('transient',[503,200]),('exhausted',[503]),('bad_alias',[200])]:
          key=f'{entry}/{operation}/{mode}/{case}';path='/'+str(len(out))+'/file';responses[path]=sequence
          with tempfile.TemporaryDirectory() as temp,ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ,{'BACKPROP_DIR':temp,'WANDB_CACHE_DIR':temp+'/cache'}))
            stack.enter_context(patch.object(w,'termlog'));stack.enter_context(patch.object(w,'termwarn'))
            stack.enter_context(patch('urllib3.util.retry.time.sleep'))
            settings=dict(silent=True,console='off',disable_git=True,x_disable_stats=True);published=[]
            if REFERENCE:
                run=w.Run(settings=w.Settings(mode=mode,entity='e',project='p',run_id='writer',**settings))
                importlib.import_module('wandb.sdk.wandb_init')._set_global_run(run)
                run._cached_public_api=SimpleNamespace(settings={'entity':'e','project':'p'},_expected_artifact_type=lambda **kw:None,_set_artifact_save_handle=lambda *args:None)
                def publish(owner,artifact,aliases,tags,*args,**kwargs):published.append(artifact);return object()
                run._interface=SimpleNamespace(publish_artifact=publish,deliver_artifact=publish,deliver_history_step=lambda:SimpleNamespace(wait_or=lambda **kw:SimpleNamespace(step=0)))
            else:
                run=w.init(id='writer',entity='e',project='p',mode=mode,dir=temp,_sink=w.MemorySink() if mode=='online' else None,_deployment={},settings={**settings,'flush_interval':600})
            cls=w.Run if entry=='wandb' else getattr(importlib.import_module(entry.rsplit('.',1)[0]),'Run')
            call=getattr(w,operation) if entry=='wandb' else getattr(cls,operation).__get__(run)
            try:
                result=call(base+path,name='http-model',aliases=['bad/name'] if case=='bad_alias' else ['release'],**({'distributed_id':'parts'} if DISTRIBUTED else {}))
                if REFERENCE:
                    entry_value=published[-1].manifest.entries['file'];size=entry_value.size;digest=entry_value.digest
                else:
                    from backprop.transport import read_journal
                    manifest=next(e['payload']['manifest'] if e['type']=='artifact_part' else e['payload'] for e in read_journal(run.sync_dir)
                        if e['type']==('artifact_part' if operation=='upsert_artifact' else 'artifact'))
                    entry_value=manifest['files'][0];size=entry_value['size'];digest=entry_value['digest']
                obs={'return':'None' if result is None else 'Artifact','size':size,'digest':digest}
            except Exception as error:
                obs={'error':type(error).__name__,'message':str(error).replace(base+path,'<url>').replace(str(server.server_port),'<port>')}
                if REFERENCE:assert not published
                else:
                    from backprop.transport import read_journal
                    assert not any(e['type'] in {'artifact','artifact_part'} for e in read_journal(run.sync_dir))
            obs['requests']=requests.get(path,0);out[key]=obs
            if REFERENCE:
                run._is_finished=True;importlib.import_module('wandb.sdk.lib.module').unset_globals()
            else:run.finish()
    finally:
        server.shutdown();server.server_close();thread.join()
    if REFERENCE:
        out['source_hashes']={}
        for module in ['wandb.sdk.artifacts.storage_policies._factories','wandb.sdk.artifacts.storage_handlers.http_handler']:
            file=Path(importlib.import_module(module).__file__)
            assert file.read_bytes()==(ROOT/'.reference/wandb/source'/Path(*module.split('.')).with_suffix('.py')).read_bytes()
            out['source_hashes'][module]=hashlib.sha256(file.read_bytes()).hexdigest()
    return out

if __name__=='__main__':print(json.dumps(build()))
