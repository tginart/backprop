"""Verify every assessed missing surface fails explicitly without side effects.

Architecture: frozen assessment coverage -> real public/alias lookups and calls ->
error identity -> before/after journals. The binary target map is not a runtime
blocklist. Supported disabled logging and useful partial APIs remain operational.
Main callers: SDK release suite; optional integration imports also run in CI.
"""
import importlib
import inspect
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import backprop as w
from backprop.compatibility import install_wandb_aliases
from backprop.unsupported import UnavailableImports, module_getattr
from backprop.unsupported_catalog import BOUNDARIES
from test_artifact_identity import SETTINGS
ROOT = Path(__file__).resolve().parents[1]


class UnsupportedContracts(unittest.TestCase):
    def test_unsupported_error_documentation_exists_in_source_distribution(self):
        with self.assertRaises(w.UnsupportedFeatureError) as caught:
            w.sweep({})
        guides=re.findall(r'docs/[A-Za-z0-9_./-]+\.md',str(caught.exception))
        self.assertTrue(guides,'Unsupported errors must link to a user-facing guide')
        for guide in guides:
            with self.subTest(guide=guide):self.assertTrue((ROOT/guide).is_file(),guide)


    def test_every_boundary_rejects_or_uses_explicit_setting_policy(self):
        install_wandb_aliases()
        field_values={'disable_job_creation':False,'sagemaker_disable':False,'identity_token_file':'token',
            'job_name':'job','job_source':'repo','launch':True,'launch_config_path':'config',
            'organization':'org','sweep_id':'sweep','sweep_param_path':'params'}
        finder=UnavailableImports()
        for api,kind in BOUNDARIES.items():
            with self.subTest(api=api):
                if api.startswith('wandb.Settings.') and kind=='field':
                    field=api.rsplit('.',1)[1]
                    with self.assertRaises(w.UnsupportedError):w.Settings(**{field:field_values[field]})
                    continue
                with self.assertRaises(w.UnsupportedError) as caught:
                    if kind in {'module','namespace'}:
                        finder.find_spec(api)
                    else:
                        value=w
                        for part in api.split('.')[1:]:value=getattr(value,part)
                        if isinstance(value,property):value.__get__(object(),object)
                        elif api.endswith('.__getattr__'):value(object(),'hosted_service_attribute')
                        else:value(object())
                self.assertIs(type(caught.exception),w.UnsupportedFeatureError)
                self.assertIsInstance(caught.exception,NotImplementedError)
                self.assertIn(api,str(caught.exception))

    def test_native_and_wandb_imports_fail_without_optional_dependencies(self):
        # A clean child verifies real import machinery, without preloaded aliases.
        source=r"""
import importlib,sys
import backprop as w
from backprop.compatibility import install_wandb_aliases
install_wandb_aliases()
for prefix in ['backprop','wandb']:
 for suffix in ['automations','integration.diffusers','integration.dspy','integration.openai.fine_tuning','integration.sagemaker','sacred','tensorflow']:
  try:importlib.import_module(prefix+'.'+suffix)
  except w.UnsupportedError as e:assert 'not implemented' in str(e)
  else:raise AssertionError(prefix+'.'+suffix)
try:importlib.import_module('wandb.sdk.launch')
except w.UnsupportedError:pass
else:raise AssertionError('Launch accepted')
assert not any(name in sys.modules for name in ['torch','tensorflow','lightgbm','diffusers'])
"""
        result=subprocess.run([sys.executable,'-B','-c',source],capture_output=True,text=True,
            cwd=tempfile.gettempdir(),env={**os.environ,'PYTHONPATH':str(ROOT/'sdk')},timeout=30)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_supported_operations_keep_working_and_rejections_write_nothing(self):
        with tempfile.TemporaryDirectory() as directory,patch.dict(os.environ,{'BACKPROP_DIR':directory}):
            sink=w.MemorySink()
            for mode in ['online','offline','disabled']:
                with w.init(id=mode,entity='e',project='p',mode=mode,dir=directory,
                        _sink=sink if mode=='online' else None,_deployment={},settings=SETTINGS) as run:
                    before=[(str(p),p.read_bytes()) for p in Path(directory).rglob('events.jsonl')]
                    for name in ['alert','link_artifact','link_model']:
                        with self.assertRaises(w.UnsupportedError):getattr(run,name)('input')
                    for field in ['sweep_id','sweep_url']:
                        with self.assertRaises(w.UnsupportedError):getattr(run,field)
                    self.assertEqual(before,[(str(p),p.read_bytes()) for p in Path(directory).rglob('events.jsonl')])
                    run.log({'loss':1.5})
                    if mode=='disabled':
                        value=run.log_artifact(w.Artifact('a','model'))
                        self.assertIs(value.wait(),value)
                        with self.assertRaises(w.UnsupportedError):value.link('registry')
                        with self.assertRaises(w.UnsupportedError):value.ttl=1
            api=w.Api._from_store(sink,entity='e',project='p')
            public=api.run('online');events=json.dumps(sink.events,sort_keys=True)
            with self.assertRaises(w.UnsupportedError):api.runs(include_sweeps=True)
            with self.assertRaises(w.UnsupportedError):api.from_path('e/p/sweeps/s')
            for name in ['sweep','sweep_name','user']:
                with self.assertRaises(w.UnsupportedError):getattr(public,name)
            self.assertEqual(json.dumps(sink.events,sort_keys=True),events)
            self.assertEqual(public.summary['loss'],1.5)

    def test_aliases_settings_mutation_and_unknown_names(self):
        install_wandb_aliases()
        from wandb.sdk.wandb_run import Run
        from wandb.sdk.artifacts.artifact import Artifact
        from wandb.apis.public.api import Api
        from wandb.errors import UnsupportedError
        self.assertIs(UnsupportedError,w.UnsupportedError)
        with self.assertRaises(UnsupportedError):_ = w.sdk.data_types.Object3D
        for value,name in [(Run,'alert'),(Artifact,'delete'),(Api,'create_user')]:
            with self.assertRaises(UnsupportedError):getattr(value,name)(object())
        settings=w.Settings()
        with self.assertRaises(UnsupportedError):settings.sweep_id='sweep'
        self.assertIsNone(settings.sweep_id)
        with self.assertRaises(AttributeError):getattr(w,'this_name_is_a_typo')
        # Disabling unsupported services is a useful, successful request.
        self.assertTrue(w.Settings(disable_job_creation=True,sagemaker_disable=True).sagemaker_disable)

    def test_partial_supported_imports_are_not_disabled_by_no_target(self):
        install_wandb_aliases()
        for api in [w.Run,w.Settings,w.Api.runs,w.Api.from_path,w.Api.create_custom_chart,w.plot_table,w.visualize]:
            self.assertTrue(callable(api));self.assertFalse(getattr(api,'__backprop_unsupported_api__',False))
        self.assertEqual(w.Artifact('a','model').type,'model')
        from types import ModuleType
        module=ModuleType('backprop.integration.lightgbm')
        module_getattr(module,'wandb.lightgbm')
        with self.assertRaises(w.UnsupportedError):_ = module.wb_telemetry
        with self.assertRaises(AttributeError):_ = module.typo

    def test_file_environment_and_bulk_settings_do_not_swallow_rejection(self):
        from types import SimpleNamespace
        import warnings
        settings=w.Settings(project='before')
        for action in [lambda:settings.update_from_dict({'project':'after','launch':True}),
                       lambda:settings.update_from_env_vars({'WANDB_PROJECT':'after','WANDB_LAUNCH':'true'}),
                       settings.update_from_system_settings]:
            with patch.object(w.Settings,'read_system_settings',return_value=SimpleNamespace(all=lambda:{'project':'after','launch':True})), warnings.catch_warnings(record=True) as seen:
                with self.assertRaises(w.UnsupportedFeatureError):action()
                self.assertEqual(settings.project,'before')
                self.assertFalse(settings.launch)
                self.assertFalse(seen)
