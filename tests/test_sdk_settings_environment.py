"""Pinned portable environment precedence and real module-launch discovery."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import backprop


class SettingsEnvironmentContracts(unittest.TestCase):
    def test_code_environment_refresh_respects_explicit_false_and_disable(self):
        for initial, environ, expected in [(None, {'WANDB_SAVE_CODE': 'yes'}, True),
                (True, {'WANDB_DISABLE_CODE': 'true'}, False),
                (False, {'WANDB_SAVE_CODE': 'true'}, False),
                (True, {'WANDB_SAVE_CODE': 'invalid'}, False)]:
            with self.subTest(initial=initial, environ=environ), patch.dict(os.environ, environ, clear=True):
                settings = backprop.Settings(save_code=initial, disable_git=True)
                settings.update_from_system_environment()
                self.assertEqual(settings.save_code, expected)
        for value, expected in [('false',False),('False',False),('0',True),('true',True)]:
            with patch.dict(os.environ, {'WANDB_DISABLE_GIT':value}, clear=True):
                settings = backprop.Settings(disable_git=not expected)
                settings.update_from_system_environment()
                self.assertEqual(settings.disable_git, expected)

    def test_host_container_program_and_path_precedence(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            script = Path(directory)/'train.py'; script.write_text('pass')
            settings=backprop.Settings(disable_git=True, program=str(script), program_relpath='selected.py', host='selected-host', docker='selected-container')
            with patch.dict(os.environ, {'WANDB_PROGRAM':'ignored.py','WANDB_DOCKER':'ignored-container'}):
                settings.update_from_system_environment()
            self.assertEqual(settings.program,str(script)); self.assertEqual(settings.program_abspath,str(script))
            self.assertEqual(settings.program_relpath,'selected.py'); self.assertEqual(settings.host,'selected-host')
            self.assertEqual(settings.docker,'selected-container')
            with patch.dict(sys.modules, {'__main__': SimpleNamespace(__spec__=None)}):
                empty=backprop.Settings(disable_git=True)
                empty.update_from_system_environment()
                self.assertEqual(empty.program,'<python with no main file>')
            with patch.dict(os.environ, {'WANDB_PROGRAM':str(script),'WANDB_DOCKER':'env-container'}):
                environment=backprop.Settings(disable_git=True)
                environment.update_from_system_environment()
                self.assertEqual(environment.program,str(script)); self.assertEqual(environment.docker,'env-container')

    def test_real_module_and_file_entry_points(self):
        sdk=str(Path(__file__).resolve().parents[1]/'sdk')
        with tempfile.TemporaryDirectory() as directory:
            script=Path(directory).resolve()/'probe_program.py'
            script.write_text('import json,backprop\ns=backprop.Settings(disable_git=True);s.update_from_system_environment()\nprint(json.dumps([s.program,s.program_abspath,s.program_relpath]))\n')
            environment={key:value for key,value in os.environ.items() if not key.startswith('WANDB_')}
            environment['PYTHONPATH']=sdk; environment['PYTHONDONTWRITEBYTECODE']='1'
            for args, expected in [(['-m','probe_program'], '-m probe_program'),([str(script)],str(script))]:
                output=subprocess.check_output([sys.executable,'-B',*args],cwd=directory,env=environment,text=True)
                values=json.loads(output); self.assertEqual(values[0],expected)
                self.assertEqual(values[1],None if args[0]=='-m' else str(script))
                self.assertEqual(values[2],None if args[0]=='-m' else 'probe_program.py')

    def test_direct_refresh_discovers_vscode_notebook_and_honors_privacy(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ,{},clear=True):
            path=Path(directory).resolve()/'train.ipynb';path.write_text('{}')
            shell=SimpleNamespace(kernel=SimpleNamespace(shell=SimpleNamespace(user_ns={'__vsc_ipynb_file__':str(path)})),config={})
            with patch.dict(sys.modules, {'IPython':SimpleNamespace(get_ipython=lambda:shell)}):
                settings=backprop.Settings(disable_git=True);settings.update_from_system_environment()
                self.assertEqual(settings.program,str(path));self.assertEqual(settings.program_abspath,str(path))
                private=backprop.Settings(disable_git=True,disable_code=True);private.update_from_system_environment()
                self.assertIsNone(private.program_abspath)


if __name__=='__main__': unittest.main()
