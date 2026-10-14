"""Compare Artifact.save routing and automatic-run arguments with pinned W&B.

Architecture: genuine Artifact and Settings -> public save -> genuine most-recent
run registry -> explicit run-start/publication/mutation service boundary. Main
caller: save contract tests. Persistence and actual automatic runs are exercised
separately against native journals, so this probe isolates routing decisions.
"""
import hashlib,importlib,inspect,json,sys,tempfile,threading
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];REFERENCE=sys.argv[1]=='reference'
if REFERENCE:
    import wandb as w
    from wandb.sdk.artifacts.artifact import ArtifactState
    from wandb.sdk.wandb_setup import _WandbSetup
else:
    import backprop as w
    from backprop.compatibility import install_wandb_aliases
    install_wandb_aliases()


class RunBoundary:
    """Supply only publication/context boundaries while exposing all routing."""
    def __init__(self,name,record,failure=None):self.name=name;self.record=record;self.failure=failure;self._closed=False;self.disabled=False
    def _telemetry_callback(self,record):pass
    def log_artifact(self,value):
        self.record.append({'publish':self.name,'same_object':value is self.artifact})
        if self.failure=='publish':raise PermissionError('publication denied')
    def __enter__(self):self.record.append({'enter':self.name});return self
    def __exit__(self,kind,value,traceback):
        self._closed=True;self.record.append({'exit':self.name,'error':kind.__name__ if kind else None})
        if self.failure=='finish':raise OSError('finish denied')


def cases():
    result={}
    for stage in ['draft','finalized','queued','committed']:
      for active in ['none','global','newer','newer_finished']:
       for settings in ['none','offline','online','disabled','dict']:
        result[f'{stage}/{active}/{settings}']={'stage':stage,'active':active,'settings':settings}
    for project in [None,'','explicit']:
      for source in [False,True]:result[f'project/{project}/{source}']={'project':project,'source':source}
    for failure in ['init','publish','finish']:result['failure/'+failure]={'failure':failure}
    for arguments in ['extra','unknown']:result['arguments/'+arguments]={'arguments':arguments}
    for active in ['none','newer']:result['incremental/'+active]={'incremental':True,'active':active}
    return result


def build():
    out={}
    for module in ['wandb','wandb.sdk','wandb.sdk.artifacts.artifact']:
      cls=importlib.import_module(module).Artifact;group={}
      for key,case in cases().items():
       with ExitStack() as stack:
        stack.enter_context(patch.object(w,'termwarn'))
        stage=case.get('stage','draft');artifact=cls('model','model',incremental=case.get('incremental',False))
        if stage in ['finalized','queued']:artifact.finalize()
        source=case.get('source',False)
        if REFERENCE:
            registry=object.__new__(_WandbSetup);registry._active_runs=[];registry._active_runs_lock=threading.Lock()
            stack.enter_context(patch('wandb.sdk.wandb_setup.singleton',return_value=registry))
            if source:artifact._source_entity='source-entity';artifact._source_project='source-project'
            if stage=='committed':artifact._state=ArtifactState.COMMITTED
        else:
            if source:artifact._entity='source-entity';artifact._project='source-project'
            artifact._logged=stage in ['queued','committed'];artifact._committed=stage=='committed'
        records=[];failure=case.get('failure');first=RunBoundary('first',records,failure);second=RunBoundary('second',records,failure)
        first.artifact=second.artifact=artifact;active=case.get('active','none')
        if REFERENCE:
            if active!='none':registry.add_active_run(first)
            if active in ['newer','newer_finished']:registry.add_active_run(second)
            if active=='newer_finished':registry.remove_active_run(second);second._closed=True
        else:
            stack.enter_context(patch.object(w,'_runs',[] if active=='none' else [first] if active=='global' else [first,second]))
            if active=='newer_finished':second._closed=True
        stack.enter_context(patch.object(w,'run',None if active=='none' else first))
        def update(*args):records.append({'update':True})
        stack.enter_context(patch.object(artifact,'_update',side_effect=update) if REFERENCE else patch('backprop.artifact_updates.save_artifact',side_effect=update))
        setting_case=case.get('settings','none')
        supplied=None if setting_case=='none' else {'mode':'offline'} if setting_case=='dict' else w.Settings(mode=setting_case,silent=False)
        def initialize(**kwargs):
            settings=kwargs['settings']
            records.append({'init':{'entity':kwargs['entity'],'project':kwargs['project'],'job_type':kwargs['job_type'],
                'supplied_settings':settings is supplied,'silent':getattr(settings,'silent',None),'mode':getattr(settings,'mode',settings.get('mode') if isinstance(settings,dict) else None)}})
            if failure=='init':raise PermissionError('authentication denied')
            created=RunBoundary('automatic',records,failure);created.artifact=artifact;return created
        stack.enter_context(patch.object(w,'init',side_effect=initialize))
        args=[];kwargs={'project':case.get('project','project-argument'),'settings':supplied}
        if case.get('arguments')=='extra':args=[None,None,1];kwargs={}
        if case.get('arguments')=='unknown':kwargs['unknown']=True
        try:
            value=artifact.save(*args,**kwargs);outcome={'return':value}
        except Exception as error:outcome={'error':type(error).__name__,'message':str(error)}
        group[key]={'outcome':outcome,'records':records}
      group['signature']=[(p.name,p.kind.name,'required' if p.default is p.empty else repr(p.default)) for p in inspect.signature(cls.save).parameters.values()]
      out[module]=group
    if REFERENCE:
        out['source_hashes']={}
        for module in ['wandb.sdk.artifacts.artifact','wandb.sdk.wandb_setup']:
            path=Path(importlib.import_module(module).__file__)
            assert path.read_bytes()==(ROOT/'.reference/wandb/source'/Path(*module.split('.')).with_suffix('.py')).read_bytes()
            out['source_hashes'][module]=hashlib.sha256(path.read_bytes()).hexdigest()
    return out

if __name__=='__main__':print(json.dumps(build()))
