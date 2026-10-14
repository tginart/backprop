"""Compare public cloud publication with genuine pinned handler algorithms.

Architecture: public Run/root calls -> actual Artifact/provider handlers ->
controlled cloud client boundary -> public error or captured manifest. Native
publication uses real journals. Main caller: test_artifact_provider_publication.
The cloud clients are fixtures; no ambient credentials or cloud network is used.
"""
import hashlib,importlib,json,os,sys,tempfile
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace,ModuleType
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];REFERENCE=sys.argv[1]=='reference'
DISTRIBUTED=len(sys.argv)>2 and sys.argv[2]=='distributed'
if REFERENCE:import wandb as w
else:
    import backprop as w
    from backprop.compatibility import install_wandb_aliases
    install_wandb_aliases()


class ClientError(Exception):
    def __init__(self,code):self.response={'Error':{'Code':code,'Message':'fixture provider failure'}}


class Objects(list):
    def filter(self,**kwargs):return self
    def limit(self,count):return Objects(self[:count])


def provider(stack,scheme,case,calls):
    """Supply only provider clients; original dispatch and handler code executes.

    Main caller: build. Errors at construction, metadata and listing boundaries
    must reach the caller before either SDK publishes an artifact record.
    """
    prefix='wandb.sdk.artifacts.storage_handlers' if REFERENCE else 'backprop.reference_handlers'
    module=importlib.import_module(prefix+'.'+scheme+'_handler')
    name={'s3':'S3Handler','gcs':'GCSHandler','azure':'AzureHandler'}[scheme];cls=getattr(module,name)
    def check(stage):
        calls.append(stage)
        if case==stage+'_failure':raise PermissionError('fixture '+stage+' denied')
    if scheme=='s3':
        def object(key):
            def load():
                check('metadata')
                if case=='missing':raise ClientError('404')
                if case=='forbidden':raise ClientError('403')
            return SimpleNamespace(key=key,e_tag='"etag"',size=5,content_length=5,version_id='7',content_type='application/octet-stream',load=load)
        def listing():
            check('listing');return Objects([] if case=='missing' else [object('file/a')])
        class Bucket:
            @property
            def objects(self):return listing()
        client=SimpleNamespace(Object=lambda bucket,key:object(key),ObjectVersion=lambda *args:SimpleNamespace(Object=lambda:object(args[1])),Bucket=lambda name:Bucket())
        def initialize(handler):
            check('init');handler._s3=client;handler._botocore=SimpleNamespace(exceptions=SimpleNamespace(ClientError=ClientError));return client
        stack.enter_context(patch.object(cls,'init_boto',initialize))
        uri='s3://bucket/'+('' if case=='listing_failure' else 'file')
    elif scheme=='gcs':
        obj=SimpleNamespace(name='file',etag='etag',size=5,generation=7)
        def get_blob(*args,**kwargs):
            check('metadata');return None if case in ['missing','listing_failure'] else obj
        def list_blobs(**kwargs):check('listing');return []
        client=SimpleNamespace(bucket=lambda name:SimpleNamespace(get_blob=get_blob,list_blobs=list_blobs))
        def initialize(handler):check('init');return client
        stack.enter_context(patch.object(cls,'init_gcs',initialize));uri='gs://bucket/file'+('#7' if case=='missing' else '')
    else:
        props=SimpleNamespace(name='file',etag='"etag"',size=5,version_id='7',metadata={},has_key=lambda key:key=='metadata')
        def exists(**kwargs):check('metadata');return case not in ['missing','listing_failure']
        def listing(**kwargs):check('listing');return []
        def service(*args,**kwargs):
            check('init');return SimpleNamespace(get_blob_client=lambda **kwargs:SimpleNamespace(exists=exists,get_blob_properties=lambda **kwargs:props),get_container_client=lambda name:SimpleNamespace(list_blobs=listing))
        blob=ModuleType('azure.storage.blob');blob.BlobServiceClient=service
        stack.enter_context(patch.object(cls,'_get_module',return_value=blob));stack.enter_context(patch.object(cls,'_get_credential',return_value='fixture-credential'))
        uri='https://account.blob.core.windows.net/container/file'
    return uri


def build():
    out={}
    for entry in (['wandb.Run','wandb.sdk.wandb_run.Run'] if DISTRIBUTED else ['wandb','wandb.Run','wandb.sdk.wandb_run.Run']):
      for operation in (['upsert_artifact','finish_artifact'] if DISTRIBUTED else ['log_artifact','log_model']):
       for mode in ['online','offline']:
        for scheme in ['s3','gcs','azure']:
         cases=['success','init_failure','metadata_failure','listing_failure','missing','bad_alias']+(['forbidden'] if scheme=='s3' else [])
         for case in cases:
          with tempfile.TemporaryDirectory() as temp,ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ,{'BACKPROP_DIR':temp,'WANDB_CACHE_DIR':temp+'/cache'}))
            stack.enter_context(patch.object(w,'termlog'));stack.enter_context(patch.object(w,'termwarn'))
            calls=[];uri=provider(stack,scheme,case,calls);published=[]
            settings=dict(silent=True,console='off',disable_git=True,x_disable_stats=True)
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
                result=call(uri,name='cloud-model',aliases=['bad/name'] if case=='bad_alias' else ['release'],**({'distributed_id':'parts'} if DISTRIBUTED else {}))
                artifact=published[-1] if REFERENCE else result
                if not REFERENCE:
                    from backprop.transport import read_journal
                    manifests=[e['payload']['manifest'] if e['type']=='artifact_part' else e['payload'] for e in read_journal(run.sync_dir)
                        if e['type']==('artifact_part' if operation=='upsert_artifact' else 'artifact')]
                    manifest=manifests[-1]
                    entries=[{'path':f['name'],'ref':f['ref'],'digest':f['digest'],'size':f.get('size'),'extra':f.get('extra',{})} for f in manifest['files']]
                    kind=manifest['artifact_type']
                else:
                    entries=[{'path':str(e.path),'ref':e.ref,'digest':e.digest,'size':e.size,'extra':dict(e.extra)} for e in artifact.manifest.entries.values()];kind=artifact.type
                obs={'return':'None' if result is None else 'Artifact','type':kind,'entries':sorted(entries,key=lambda e:e['path'])}
            except Exception as error:
                obs={'error':'Error' if type(error) is w.Error else type(error).__name__,'message':str(error)}
                if REFERENCE:assert not published
                else:
                    from backprop.transport import read_journal
                    assert not any(e['type'] in {'artifact','artifact_part'} for e in read_journal(run.sync_dir))
            obs['calls']=calls;out[f'{entry}/{operation}/{mode}/{scheme}/{case}']=obs
            if REFERENCE:
                run._is_finished=True;importlib.import_module('wandb.sdk.lib.module').unset_globals()
            else:run.finish()
    if REFERENCE:
        out['source_hashes']={}
        for module in ['wandb.sdk.wandb_run',*[f'wandb.sdk.artifacts.storage_handlers.{name}_handler' for name in ['s3','gcs','azure']]]:
            path=Path(importlib.import_module(module).__file__)
            assert path.read_bytes()==(ROOT/'.reference/wandb/source'/Path(*module.split('.')).with_suffix('.py')).read_bytes()
            out['source_hashes'][module]=hashlib.sha256(path.read_bytes()).hexdigest()
    return out

if __name__=='__main__':print(json.dumps(build(),sort_keys=True))
