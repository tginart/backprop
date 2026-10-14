"""Compare real public publication methods with explicit hosted boundaries.

Architecture: genuine constructors/files -> decorated public operation -> hosted
publication/type-query fixture -> independent observations. Native publication
uses actual journals. Main caller: test_artifact_logging and release evidence.
Transport None versus empty tags both mean no tags on a fresh artifact; they are
normalized here, while native existing-object edits have separate tests.
"""
import hashlib, importlib, inspect, json, os, sys, tempfile
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
REFERENCE = sys.argv[1] == 'reference'
if REFERENCE:
    import wandb as w
else:
    import backprop as w
    from backprop.compatibility import install_wandb_aliases
    install_wandb_aliases()

def cases():
    out = {}
    for value in ['file', 'directory', 'trailing_directory', 'pathlib', 'file_uri', 'missing', 'integer', 'bytes', 'none', 'object', 'finalized', 'incremental', 'queued']:
        out[value] = {'value': value}
    for option, values in {'name': [None, '', 'custom', 'bad/name'], 'type': [None, '', 'data', 'model'], 'aliases': [None, [], ['best'], 'best', ['bad/name']], 'tags': [None, [], ['trained'], 'trained', ['bad/tag'], [3], b'tag', 3, [['tag']], ['newline\n'], ['dup','dup']]}.items():
        for value in values:
            out[option + '/' + repr(value)] = {'value': 'file', option: value}
    for mode in ['online', 'offline', 'disabled', 'finished']:
        for kind in ['file', 'object', 'incremental', 'missing_args', 'unknown_arg', 'extra_args']:
            out[mode + '/' + kind] = {'mode': mode, 'value': kind}
    out['object_name_type_ignored']={'value':'object','name':'bad/name','type':'wrong'}
    out['existing_type']={'value':'object','expected_type':'wrong'}
    out['aliases_before_type']={'value':'object','expected_type':'wrong','aliases':['bad/name']}
    out['tags_before_path']={'value':'missing','tags':['bad/tag']}
    return out

out = {}
for label in ['wandb.Run', 'wandb.sdk.wandb_run.Run', 'wandb']:
    for operation in ['log_artifact', 'log_model']:
        cls = w.Run if label == 'wandb' else getattr(importlib.import_module(label.rsplit('.', 1)[0]), 'Run')
        group = {}
        for key, case in cases().items():
            if operation == 'log_model' and ('tags' in case or 'type' in case):
                continue
            with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
                folder = Path(directory)
                stack.enter_context(patch.dict(os.environ, {'BACKPROP_DIR': directory, 'WANDB_CACHE_DIR': str(folder / 'reference-cache')}))
                stack.enter_context(patch.object(w, 'termwarn'))
                path = folder / 'weights.bin'; path.write_bytes(b'model')
                tree = folder / 'tree'; tree.mkdir(); (tree / 'file.txt').write_text('tree')
                mode = case.get('mode', 'online')
                settings = dict(silent=True, console='off', disable_git=True, x_disable_stats=True)
                records = []
                if REFERENCE:
                    if mode == 'disabled':
                        run = w.init(mode='disabled', settings=settings)
                    else:
                        run = cls(settings=w.Settings(mode='offline' if mode == 'offline' else 'online', run_id='writer', entity='e', project='p', **settings))
                        importlib.import_module('wandb.sdk.wandb_init')._set_global_run(run)
                        api = SimpleNamespace(settings={'entity':'e','project':'p'}, _expected_artifact_type=lambda **kwargs: case.get('expected_type'), _set_artifact_save_handle=lambda *args: None)
                        run._cached_public_api = api
                        def publish(owner, artifact, aliases, tags, *args, **kwargs):
                            records.append((artifact, aliases, tags)); return object()
                        run._interface = SimpleNamespace(deliver_artifact=publish, publish_artifact=publish,
                            deliver_history_step=lambda: SimpleNamespace(wait_or=lambda **kwargs: SimpleNamespace(step=0)))
                else:
                    run = w.init(id='writer', entity='e', project='p', mode=mode if mode in ['offline','disabled'] else 'online', dir=directory, _sink=None if mode in ['offline','disabled'] else w.MemorySink(), _deployment={}, settings={**settings,'flush_interval':600})
                kind = case['value']
                if kind in ['object','finalized','incremental','queued']:
                    value = w.Artifact('draft', 'data', incremental=kind=='incremental')
                    value.add_file(str(path))
                    if kind=='finalized': value.finalize()
                    if kind=='queued': run.log_artifact(value, aliases=['first'])
                else:
                    value = {'file':str(path),'directory':str(tree),'trailing_directory':str(tree)+'/', 'pathlib':path, 'file_uri':path.as_uri(), 'missing':str(folder/'absent'), 'integer':3,'bytes':b'file','none':None}.get(kind, str(path))
                if not REFERENCE and case.get('expected_type'):
                    run._sink.events.append({'schema_version':1,'event_id':'prior','timestamp':'2026-10-01T00:00:00+00:00','entity':'e','project':'p','run_id':'prior','type':'artifact','payload':{'artifact_id':'prior','name':'draft','artifact_type':case['expected_type'],'version':'sha256-prior','files':[],'aliases':[]}})
                args = [value]
                kwargs = {k:v for k,v in case.items() if k in ['name','type','aliases','tags']}
                if kind=='missing_args': args=[]
                if kind=='unknown_arg': kwargs['unknown']=True
                if kind=='extra_args': args=[value,None,None,None,None,None]
                if mode=='finished':
                    if REFERENCE: run._is_finished=True
                    else: run.finish()
                call = getattr(w if label=='wandb' else run, operation)
                try:
                    result=call(*args,**kwargs)
                    entry={'return':'None' if result is None else 'Noop' if mode=='disabled' else 'Artifact'}
                    if REFERENCE:
                        if records:
                            artifact, aliases, tags = records[-1]
                            entry.update(name=artifact.name,type=artifact.type,files=sorted(artifact.manifest.entries),aliases=sorted(aliases),tags=tags or [])
                    elif run._journal:
                        from backprop.transport import read_journal
                        manifests=[e['payload'] for e in read_journal(run.sync_dir) if e['type']=='artifact']
                        if manifests:
                            manifest=manifests[-1]
                            updates=[e['payload'] for e in read_journal(run.sync_dir) if e['type']=='artifact_update' and e['payload']['artifact_id']==manifest['artifact_id']]
                            aliases=updates[-1]['alias_add'] if updates else manifest['aliases']
                            tags=updates[-1]['values']['tags'] if updates else manifest['tags']
                            entry.update(name=manifest['name'],type=manifest['artifact_type'],files=sorted(f['name'] for f in manifest['files']),aliases=sorted(aliases),tags=tags)
                except Exception as error:
                    entry={'error':'Error' if type(error) is w.Error else type(error).__name__,'message':str(error).replace(directory,'<tmp>')}
                group[key]=entry
                if REFERENCE:
                    run._is_finished=True
                    importlib.import_module('wandb.sdk.lib.module').unset_globals()
                else: run.finish()
        group['signature']=[(p.name,p.kind.name,'required' if p.default is p.empty else repr(p.default)) for p in inspect.signature(getattr(w if label=='wandb' else cls,operation)).parameters.values()]
        out[label+'.'+operation]=group
if REFERENCE:
    out['source_hashes']={}
    for module in ['wandb.sdk.wandb_run','wandb.sdk.artifacts.artifact','wandb.sdk.artifacts._validators','wandb.sdk.lib.noop_run']:
        actual=Path(importlib.import_module(module).__file__)
        expected=ROOT/'.reference/wandb/source'/Path(*module.split('.')).with_suffix('.py')
        assert actual.read_bytes()==expected.read_bytes()
        out['source_hashes'][module]=hashlib.sha256(actual.read_bytes()).hexdigest()
print(json.dumps(out,sort_keys=True))
