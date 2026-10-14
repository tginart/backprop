"""Verify durable Foundry authentication without user credentials or networking.

Architecture: temporary owner-private cache -> public login/init/Api factories ->
separate-process restart/concurrent writes -> real terminal input and cleanup.
Pinned source tests compare relogin selection independently of backend storage.
Main callers: core SDK CI and local unittest discovery. All tokens are fixtures.
"""
from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import backprop
from backprop import credential_store
from backprop.login_session import authenticate_candidate, AuthCandidate

ROOT = Path(__file__).resolve().parents[1]
HOST = 'https://credentials.invalid'
OTHER = 'https://other.invalid'
CONFIG = {'host': HOST, 'stream_dataset_rid': 'ri.foundry.main.dataset.credentials'}


class CredentialContracts(unittest.TestCase):
    def setUp(self):
        backprop.teardown()
        self.directory = tempfile.TemporaryDirectory(); self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'credentials.json'
        self.addCleanup(patch.stopall); self.addCleanup(backprop.teardown)
        patch.dict(os.environ, {}, clear=True).start()
        patch('backprop.settings.default_path', return_value=str(self.path)).start()
        self.loader = patch.object(backprop, 'load_config', return_value=CONFIG).start()
        self.factory = patch.object(backprop, 'native_store', return_value=SimpleNamespace(verify_credentials=lambda: None)).start()

    def write(self, token='cached-fixture', host=HOST):
        credential_store.write_token(self.path, host, token)

    def test_explicit_login_persists_only_its_foundry_host_and_survives_a_new_process(self):
        self.assertTrue(backprop.login(key='saved-fixture', host=HOST))
        self.assertEqual(credential_store.read_token(self.path, HOST.upper().replace('HTTPS', 'https')+'/'), 'saved-fixture')
        self.assertIsNone(credential_store.read_token(self.path, OTHER))
        if os.name == 'posix': self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        code = '''import os, backprop
backprop.setup({'credentials_file': os.environ['WANDB_CREDENTIALS_FILE']})
assert backprop.login(host='https://credentials.invalid', prompt=False, verify=False)
assert os.environ['BACKPROP_TOKEN']=='saved-fixture'
assert backprop.Api
print('restart-ok')
'''
        environment = {'PATH': os.defpath, 'PYTHONPATH': str(ROOT/'sdk'), 'WANDB_CREDENTIALS_FILE': str(self.path)}
        result = subprocess.run([sys.executable, '-B', '-c', code], env=environment, text=True, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'restart-ok')
        self.assertNotIn('saved-fixture', result.stderr)

    def test_default_path_and_environment_override_do_not_use_wandb_storage(self):
        self.assertTrue(credential_store.default_path().endswith('/.config/backprop/credentials.json'))
        selected = Path(self.directory.name)/'alternate.json'
        with patch.dict(os.environ, {'WANDB_CREDENTIALS_FILE': str(selected)}):
            self.assertTrue(backprop.login(key='alternate-fixture', host=HOST))
        self.assertEqual(credential_store.read_token(selected, HOST), 'alternate-fixture')
        self.assertFalse(self.path.exists())

    def test_precedence_and_relogin_skip_reusable_tokens_but_explicit_key_wins(self):
        self.write()
        selected = backprop.Settings(api_key='settings-fixture', base_url=HOST, credentials_file=str(self.path))
        def choose(**options):
            return authenticate_candidate(key=None, host=HOST, prompt=False, verify=False, timeout=None,
                                          settings=selected, load_config=lambda: CONFIG, native_store=self.factory, **options)
        with patch.dict(os.environ, {'FOUNDRY_TOKEN': 'environment-fixture'}):
            self.assertEqual(choose().token, 'environment-fixture')
            self.assertIsNone(choose(relogin=True))
            self.assertTrue(backprop.login(key='replacement-fixture', relogin=True, host=HOST, verify=False))
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(choose().token, 'replacement-fixture')
            selected.relogin = True
            self.assertIsNone(choose())
            self.assertEqual(choose(relogin=False).token, 'replacement-fixture')
        self.factory.assert_not_called()

    def test_relogin_prompt_false_preserves_cache_and_existing_session(self):
        self.write()
        old = self.path.read_bytes()
        with patch.dict(os.environ, {'BACKPROP_TOKEN': 'old-fixture', 'BACKPROP_HOST': HOST}):
            for mode in [None, True]:
                backprop.setup({'credentials_file': str(self.path), 'relogin': True})
                previous = backprop._setup_settings
                self.assertFalse(backprop.login(relogin=mode, prompt=False))
                self.assertIs(backprop._setup_settings, previous)
                self.assertEqual(os.environ['BACKPROP_TOKEN'], 'old-fixture')
                self.assertEqual(self.path.read_bytes(), old)

    def test_failed_verification_and_failed_persistence_preserve_prior_credentials(self):
        self.write()
        old = self.path.read_bytes()
        with patch.dict(os.environ, {'BACKPROP_TOKEN': 'old-session', 'BACKPROP_HOST': HOST}):
            self.factory.return_value.verify_credentials = lambda: (_ for _ in ()).throw(ValueError('rejected'))
            with self.assertRaises(backprop.AuthenticationError): backprop.login(key='new-fixture', host=HOST)
            self.assertEqual(self.path.read_bytes(), old)
            self.assertEqual(os.environ['BACKPROP_TOKEN'], 'old-session')
            with patch('backprop.credential_store.os.replace', side_effect=OSError('fixture failure')):
                with self.assertRaises(backprop.AuthenticationError): backprop.login(key='new-fixture', host=HOST, verify=False)
            self.assertEqual(self.path.read_bytes(), old)
            self.assertEqual(os.environ['BACKPROP_TOKEN'], 'old-session')
            self.assertFalse(list(self.path.parent.glob('.backprop-credentials-*')))

    def test_invalid_cache_never_falls_back_or_overwrites_its_contents(self):
        for value in ['invalid-json-secret', json.dumps({'version': 1, 'hosts': {HOST: 'bad token'}}),
                      json.dumps({'version': True, 'hosts': {}}), json.dumps({'credentials': {'secret': 'foreign-format'}})]:
            self.path.write_text(value); self.path.chmod(0o600)
            with self.assertRaises(backprop.AuthenticationError) as error:
                credential_store.read_token(self.path, HOST)
            self.assertNotIn(value, str(error.exception))
            with self.assertRaises(backprop.AuthenticationError): self.write('new-fixture')
            self.assertEqual(self.path.read_text(), value)
        self.path.unlink()
        self.assertIsNone(credential_store.read_token(self.path, HOST))

    @unittest.skipUnless(os.name == 'posix', 'POSIX permission and symlink contract')
    def test_insecure_permissions_and_symlinks_are_rejected(self):
        self.write(); self.path.chmod(0o644)
        with self.assertRaises(backprop.AuthenticationError): credential_store.read_token(self.path, HOST)
        self.path.chmod(0o600)
        alias = self.path.parent/'symlink.json'; alias.symlink_to(self.path)
        with self.assertRaises(backprop.AuthenticationError): credential_store.read_token(alias, HOST)
        with self.assertRaises(backprop.AuthenticationError): credential_store.write_token(alias, HOST, 'changed')
        self.assertEqual(credential_store.read_token(self.path, HOST), 'cached-fixture')

    def test_concurrent_processes_preserve_every_host_entry(self):
        code = 'import sys; from backprop.credential_store import write_token; write_token(sys.argv[1], sys.argv[2], sys.argv[3])'
        children = [subprocess.Popen([sys.executable, '-B', '-c', code, str(self.path), f'https://host{i}.invalid', f'token{i}'],
                                    env={'PATH': os.defpath, 'PYTHONPATH': str(ROOT/'sdk')}, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    for i in range(6)]
        try:
            for child in children:
                out, error = child.communicate(timeout=20)
                self.assertEqual(child.returncode, 0, error.decode())
        finally:
            for child in children:
                if child.poll() is None: child.kill(); child.communicate()
        for i in range(6): self.assertEqual(credential_store.read_token(self.path, f'https://host{i}.invalid'), f'token{i}')

    def test_init_and_api_preserve_selected_session_with_explicit_key_precedence(self):
        runtime = importlib.import_module('backprop.run')
        public = importlib.import_module('backprop.apis.public.api')
        self.write()
        sink = backprop.MemorySink()
        with patch.object(runtime, 'native_store', return_value=sink) as factory:
            for key in [None, 'explicit-run-fixture']:
                settings = {'credentials_file': str(self.path), 'console': 'off', 'silent': True, 'x_disable_stats': True}
                if key: settings['api_key'] = key
                run = backprop.init(project='credentials', settings=settings, dir=self.directory.name, _deployment=CONFIG)
                self.assertEqual(factory.call_args.kwargs['token'], key or 'cached-fixture')
                run.log({'loss': 1}); run.finish()
        with patch.object(public, 'load_config', return_value=CONFIG), patch.object(public, 'FoundryStore', return_value=sink) as factory, patch.object(sink, 'verify_credentials', return_value=True, create=True) as verify:
            # Public init selected the latest run key for this host. Api reuses
            # that session ahead of disk credentials, as pinned W&B does.
            for key in [None, 'explicit-api-fixture', None]:
                api = backprop.Api(api_key=key)
                self.assertEqual(api.api_key, key or 'explicit-run-fixture')
                self.assertEqual(factory.call_args.kwargs['token'], api.api_key)
            backprop.teardown()
            self.assertEqual(backprop.Api().api_key, 'cached-fixture')
            self.assertEqual(verify.call_count, 4)
            # Pinned Api uses a truthy explicit key; an empty key follows the
            # ordinary host-scoped credential path, independently verified by
            # test_public_api_auth_contracts with the actual reference client.
            self.assertEqual(backprop.Api(api_key='').api_key, 'cached-fixture')
            self.assertEqual(verify.call_count, 5)
            with patch('backprop.login_prompt.prompt_token', side_effect=backprop.UsageError('no terminal')):
                with self.assertRaises(backprop.UsageError): backprop.Api(overrides={'base_url': OTHER})
        serialized = repr(sink.events)
        for value in ['cached-fixture', 'explicit-run-fixture', 'explicit-api-fixture']:
            self.assertNotIn(value, serialized)

    def test_offline_modes_skip_cache_prompt_and_relogin_while_online_honors_prompt_choice(self):
        with patch('backprop.credential_store.read_token', side_effect=AssertionError('no credential read')), patch('backprop.login_prompt.prompt_token', side_effect=AssertionError('no prompt')):
            for mode in ['offline', 'dryrun', 'disabled']:
                run = backprop.init(project='local-auth', mode=mode, dir=self.directory.name,
                                    settings={'relogin': True, 'credentials_file': '/unused/credentials', 'console': 'off', 'silent': True})
                run.finish()
        with patch.dict(os.environ, {'FOUNDRY_TOKEN': 'old-fixture'}), patch('backprop.login_prompt.prompt_token', return_value=None) as prompt:
            run = backprop.init(project='offline-choice', mode='shared', dir=self.directory.name, _deployment=CONFIG,
                                settings={'relogin': True, 'console': 'off', 'silent': True})
            self.assertTrue(run.offline); self.assertFalse(run._shared)
            run.finish(); prompt.assert_called_once()

    def test_prompt_errors_retry_and_timeout_switches_to_disabled_without_cache_changes(self):
        self.write(); old = self.path.read_bytes()
        with patch('backprop.login_prompt.prompt_token', side_effect=['invalid token', 'prompt-fixture']) as prompt, patch.object(backprop, 'termwarn'):
            self.assertTrue(backprop.login(relogin=True, host=HOST, force=True, referrer='test', timeout=2))
            self.assertEqual(prompt.call_count, 2)
            prompt.assert_called_with(HOST, force=True, timeout=2, referrer='test')
        self.assertEqual(credential_store.read_token(self.path, HOST), 'prompt-fixture')
        old = self.path.read_bytes(); backprop.teardown()
        with patch('backprop.login_prompt.prompt_token', side_effect=TimeoutError()), patch.object(backprop, 'termwarn'):
            self.assertFalse(backprop.login(relogin=True, host=HOST))
        self.assertEqual(backprop._setup_settings.mode, 'disabled')
        self.assertEqual(self.path.read_bytes(), old)

    def test_pinned_selection_matches_in_independent_reference_and_native_processes(self):
        if not (ROOT/'.reference/wandb/source').is_dir(): self.skipTest('Pinned W&B source required')
        results = []
        for kind in ['reference', 'native']:
            output = subprocess.run([sys.executable, '-B', str(ROOT/'tests/auth_selection_probe.py'), kind],
                env={'PATH':os.defpath, 'PYTHONPATH':str(ROOT/'sdk')}, text=True, capture_output=True, timeout=20)
            self.assertEqual(output.returncode, 0, output.stderr)
            results.append(json.loads(output.stdout))
        self.assertEqual(len(results[0]), 24)
        self.assertEqual(results[0], results[1])

    def test_unavailable_verification_does_not_repeat_the_prompt(self):
        self.factory.return_value = object()
        with patch('backprop.login_prompt.prompt_token', return_value='prompt-fixture') as prompt:
            with self.assertRaises(backprop.AuthenticationError): backprop.login(relogin=True, host=HOST)
            prompt.assert_called_once()
        self.assertFalse(self.path.exists())

    def test_public_import_aliases_share_authentication_behavior_in_an_isolated_process(self):
        code = """import os, backprop
from backprop.compatibility import install_wandb_aliases
install_wandb_aliases()
import wandb
from wandb.sdk import login
from wandb.sdk.wandb_settings import Settings
assert login is wandb.login is backprop.login
assert Settings is backprop.Settings
wandb.setup(Settings(credentials_file=os.environ['WANDB_CREDENTIALS_FILE'], relogin=True))
assert login(host='https://credentials.invalid', prompt=False) is False
assert login(key='alias-fixture', host='https://credentials.invalid', verify=False) is True
assert os.environ['BACKPROP_TOKEN']=='alias-fixture'
print('aliases-ok')
"""
        result = subprocess.run([sys.executable, '-B', '-c', code],
            env={'PATH':os.defpath, 'PYTHONPATH':str(ROOT/'sdk'), 'WANDB_CREDENTIALS_FILE':str(self.path)},
            text=True, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'aliases-ok')
        self.assertEqual(credential_store.read_token(self.path, HOST), 'alias-fixture')

    def test_pinned_relogin_selection_and_force_prompt_arguments(self):
        from test_sdk_login_session import source_function, AUTH
        class HostUrl:
            def __init__(self, value): self.url = value
        for relogin in [False, True]:
            for prompt in [False, True]:
                for force in [False, True]:
                    calls = []
                    reference = source_function(AUTH, 'authenticate_session', {
                        'HostUrl': HostUrl, 'session_credentials': lambda **kwargs: None,
                        '_use_system_auth': lambda **kwargs: 'cached-fixture',
                        '_use_prompted_auth': lambda **kwargs: calls.append(kwargs) or 'prompt-fixture',
                    })
                    expected = reference(host=HOST, source='test', relogin=relogin, prompt=prompt,
                                         no_offline=force, no_create=force, input_timeout=3, referrer='fixture')
                    self.write()
                    with patch('backprop.login_prompt.prompt_token', return_value='prompt-fixture') as native_prompt:
                        actual = authenticate_candidate(key=None, host=HOST, prompt=prompt, verify=False,
                            relogin=relogin, force=force, timeout=3, referrer='fixture', settings=backprop.Settings(),
                            load_config=lambda: CONFIG, native_store=self.factory)
                    self.assertEqual(actual.token if actual else None, expected)
                    self.assertEqual(native_prompt.call_count, len(calls))
                    if calls: native_prompt.assert_called_with(HOST, force=force, timeout=3, referrer='fixture')


@unittest.skipUnless(os.name == 'posix', 'Actual POSIX terminal verification')
class TerminalCredentialContracts(unittest.TestCase):
    def prompt_process(self, scenario):
        import pty, select, time
        with tempfile.TemporaryDirectory() as directory:
            master, slave = pty.openpty()
            code = '''import json, os, sys, termios, backprop
backprop.setup({'credentials_file': sys.argv[1]})
before=termios.tcgetattr(0)
try:
 result=backprop.login(host='https://credentials.invalid', relogin=True, force=sys.argv[2]=='force',
                       verify=False, timeout=.2 if sys.argv[2]=='timeout' else 5, referrer='test & value')
 outcome={'result':result,'mode':backprop._setup_settings.mode,'token_matches':os.environ.get('BACKPROP_TOKEN')=='terminal-fixture'}
except EOFError:
 outcome={'error':'EOFError'}
finally:
 after=termios.tcgetattr(0)
outcome['restored']=before==after
print(json.dumps(outcome))
'''
            child = subprocess.Popen([sys.executable, '-B', '-c', code, str(Path(directory)/'credentials.json'), scenario],
                                     stdin=slave, stderr=slave, stdout=subprocess.PIPE,
                                     env={'PATH': os.defpath, 'PYTHONPATH': str(ROOT/'sdk')})
            os.close(slave); transcript = bytearray(); sent_menu = sent_token = False
            try:
                deadline = time.monotonic()+10
                while child.poll() is None:
                    self.assertLess(time.monotonic(), deadline, 'terminal child did not finish')
                    if select.select([master], [], [], .1)[0]:
                        try: transcript.extend(os.read(master, 4096))
                        except OSError: break
                    if not sent_menu and b'Continue offline:' in transcript and scenario != 'timeout':
                        os.write(master, b'2\n' if scenario == 'offline' else b'1\n'); sent_menu = True
                    if not sent_token and b'token (hidden):' in transcript:
                        os.write(master, b'\x04' if scenario == 'eof' else b'terminal-fixture\n'); sent_token = True
                output = child.communicate(timeout=5)[0]
                self.assertEqual(child.returncode, 0, transcript.decode(errors='replace'))
                self.assertNotIn(b'terminal-fixture', transcript)
                self.assertIn(b'ref=test+%26+value', transcript)
                result = json.loads(output)
                self.assertTrue(result['restored'])
                return result, transcript.decode(errors='replace')
            finally:
                os.close(master)
                if child.poll() is None: child.kill(); child.communicate()

    def test_hidden_token_and_force_skips_offline_menu(self):
        for scenario in ['normal', 'force']:
            result, transcript = self.prompt_process(scenario)
            self.assertTrue(result['result']); self.assertTrue(result['token_matches'])
            self.assertEqual('Continue offline' in transcript, scenario != 'force')

    def test_offline_timeout_and_eof_restore_terminal(self):
        for scenario in ['offline', 'timeout', 'eof']:
            result, _ = self.prompt_process(scenario)
            if scenario == 'eof': self.assertEqual(result['error'], 'EOFError')
            else:
                self.assertFalse(result['result'])
                self.assertEqual(result['mode'], 'disabled' if scenario == 'timeout' else 'offline')


if __name__ == '__main__': unittest.main()
