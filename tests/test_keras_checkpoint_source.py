"""Test checkpoint error boundaries from verified source without TensorFlow.

Architecture: extract only the storage helper's unchanged function body -> real
temporary paths and explicit storage errors. Main callers: local SDK tests when
TensorFlow is absent. Actual public hooks and Keras saves run independently in CI.
This fixture does not certify framework behavior or replace the real comparisons.
"""
import ast
import hashlib
import importlib.metadata
import json
import subprocess
import sys
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

ROOT=Path(__file__).resolve().parents[1]

def helper(path,owner):
    """Load the actual helper body with only its storage boundary supplied."""
    tree=ast.parse(path.read_text());cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='WandbModelCheckpoint')
    method=next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=='_log_ckpt_as_artifact')
    program=ast.fix_missing_locations(ast.Module(body=[ast.ImportFrom(module='__future__',names=[ast.alias(name='annotations')],level=0),method],type_ignores=[]))
    namespace={'os':os,'wandb':owner};exec(compile(program,str(path),'exec'),namespace)
    return namespace[method.name]

class KerasCheckpointSource(unittest.TestCase):
    def test_missing_best_only_save_is_skipped_and_storage_errors_propagate(self):
        native=ROOT/'sdk/backprop/integration/keras/callbacks/model_checkpoint.py'
        reference=ROOT/'.reference/wandb/source/wandb/integration/keras/callbacks/model_checkpoint.py'
        if not reference.exists():self.skipTest('Pinned source checkout unavailable')
        assert hashlib.sha256(reference.read_bytes()).hexdigest() == '224b20e720efe4bfaec5109ed564fb30e40fed24b6f697aa18c9c38e94835079'
        calls=[]
        class Artifact:
            def __init__(self,*args,**kwargs):pass
            def add_file(self,path):calls.append(path)
            def add_dir(self,path):calls.append(path)
        def rejected(*args,**kwargs):raise ValueError('storage rejected artifact')
        owner=SimpleNamespace(run=SimpleNamespace(id='run'),Artifact=Artifact,log_artifact=rejected)
        with tempfile.TemporaryDirectory() as directory:
            missing=str(Path(directory)/'skipped.keras')
            original=helper(reference,owner);current=helper(native,owner)
            with self.assertRaises(FileNotFoundError):original(SimpleNamespace(save_best_only=True),missing)
            self.assertIsNone(current(SimpleNamespace(save_best_only=True),missing));self.assertEqual(calls,[])
            with self.assertRaises(FileNotFoundError):current(SimpleNamespace(save_best_only=False),missing)
            existing=Path(directory)/'saved.keras';existing.write_bytes(b'checkpoint')
            self.assertIsNone(original(SimpleNamespace(save_best_only=False),str(existing)))
            with self.assertRaisesRegex(ValueError,'storage rejected artifact'):current(SimpleNamespace(save_best_only=False),str(existing))


    def test_table_validation_errors_match_pinned_messages(self):
        try:reference=importlib.metadata.distribution('wandb')
        except importlib.metadata.PackageNotFoundError:self.skipTest('Pinned W&B wheel unavailable')
        self.assertEqual(reference.version,'0.30.0')
        script="""
import json,sys
if sys.argv[1]=='reference':import wandb as w
else:import backprop as w
out=[]
for kwargs in [{'columns':42},{'columns':('x',)},{'data':42},{'data':((1,),)}]:
    try:w.Table(**kwargs)
    except Exception as error:out.append([type(error).__name__,str(error)])
print(json.dumps(out))
"""
        outputs=[]
        for mode in ['reference','native']:
            result=subprocess.run([sys.executable,'-B','-c',script,mode],cwd=tempfile.gettempdir(),text=True,capture_output=True,
                env={**os.environ,'PYTHONPATH':str(reference.locate_file('')) if mode=='reference' else str(ROOT/'sdk')})
            self.assertEqual(result.returncode,0,result.stderr);outputs.append(json.loads(result.stdout))
        self.assertEqual(outputs[0],outputs[1]);self.assertEqual(len(outputs[0]),4)

    def test_declared_pathlike_reaches_framework_constructor_as_string(self):
        path=ROOT/'sdk/backprop/integration/keras/callbacks/model_checkpoint.py'
        cls=next(node for node in ast.parse(path.read_text()).body if isinstance(node,ast.ClassDef) and node.name=='WandbModelCheckpoint')
        class Parent:
            def __init__(self,**kwargs):self.received=kwargs;self.save_best_only=kwargs['save_best_only']
        program=ast.fix_missing_locations(ast.Module(body=[ast.ImportFrom(module='__future__',names=[ast.alias(name='annotations')],level=0),cls],type_ignores=[]))
        namespace={'os':os,'callbacks':SimpleNamespace(ModelCheckpoint=Parent),'wandb':SimpleNamespace(run=object())}
        exec(compile(program,str(path),'exec'),namespace)
        callback=namespace['WandbModelCheckpoint'](Path('models/checkpoint.keras'))
        self.assertEqual(callback.received['filepath'],'models/checkpoint.keras')
