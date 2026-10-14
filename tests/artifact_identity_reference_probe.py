"""Exercise public artifact identity methods against pinned source-backed state.

Architecture: real constructors -> explicit hosted-state fixture -> unchanged
public methods/errors -> JSON observations. Main caller: artifact identity
contract tests. Only server-assigned state/identities are injected; lifecycle
gates and serializers execute in the actual pinned package.
"""
import hashlib,importlib,inspect,json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
REFERENCE=sys.argv[1]=='reference'
if REFERENCE:
 import wandb as w
 from wandb.sdk.artifacts.artifact import ArtifactState
 assert w.__version__=='0.30.0'
else:
 import backprop as w
 from backprop.compatibility import install_wandb_aliases
 install_wandb_aliases()


def artifact(cls,state):
 """Assign the same opaque identity at the hosted-service fixture boundary."""
 value=cls('dataset','data',metadata={'description':'not included'})
 if state=='finalized':value.finalize()
 if state in ['queued','committed']:
  value._id='artifact-identity';value._version='sha256-test-version'
  value._entity='team';value._project='project'
  if REFERENCE:
   value._name='dataset:sha256-test-version';value._source_name=value._name
   value._source_version=value._version;value._commit_hash='immutable-content-digest'
   value._state=ArtifactState.COMMITTED if state=='committed' else ArtifactState.PENDING
  else:
   value._logged=True;value._committed=state=='committed';value._stored_digest='immutable-content-digest'
 return value


def attempt(value,method,call):
 try:return {'value':call()}
 except Exception as error:
  return {'error':type(error).__name__,'attribute_error':isinstance(error,AttributeError),
   'message':str(error),'name':getattr(error,'name',None),'owns_object':getattr(error,'obj',None) is value}


out={'imports':{}}
for prefix in ['wandb','wandb.sdk','wandb.sdk.artifacts.artifact']:
 cls=importlib.import_module(prefix).Artifact;out['imports'][prefix]={}
 for state in ['draft','finalized','queued','committed']:
  value=artifact(cls,state)
  out['imports'][prefix][state]={
   'json_encode':attempt(value,'json_encode',value.json_encode),
   'commit_hash':attempt(value,'commit_hash',lambda:value.commit_hash),
   'extra_argument':attempt(value,'json_encode',lambda:value.json_encode(1)),
   'unknown_argument':attempt(value,'json_encode',lambda:value.json_encode(unknown=True))}
  if state=='committed':
   first=value.json_encode();first['id']='modified';second=value.json_encode()
   out['imports'][prefix][state]['fresh_mapping']=second['id']=='artifact-identity'
 module=importlib.import_module('wandb.sdk.artifacts.exceptions')
 out['imports'][prefix]['exception_hierarchy']=issubclass(module.ArtifactNotLoggedError,module.ArtifactStatusError)
 out['imports'][prefix]['signature']=[(p.name,p.kind.name,'required' if p.default is p.empty else repr(p.default)) for p in inspect.signature(cls.json_encode).parameters.values()]
if REFERENCE:
 import wandb.sdk.artifacts.artifact as source
 actual=Path(source.__file__);expected=ROOT/'.reference/wandb/source/wandb/sdk/artifacts/artifact.py'
 assert actual.read_bytes()==expected.read_bytes()
 out['source_sha256']=hashlib.sha256(actual.read_bytes()).hexdigest()
print(json.dumps(out,sort_keys=True))
