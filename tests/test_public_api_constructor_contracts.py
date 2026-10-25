"""Constructor defaults, transport settings and verification against W&B0.30.0.

Architecture: independent installed reference and native processes -> real Api
construction at an explicit remote boundary -> retained observations. Main caller:
base SDK CI. Missing optional reference execution is a visible verification skip.
"""
import importlib.metadata
import importlib
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

import backprop
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

from framework_gap_process import run_observation
from reference_runtime import reference_runtime

ROOT = Path(__file__).resolve().parents[1]


class PublicApiConstructorContracts(unittest.TestCase):
    def observe(self, scenario):
        if not os.environ.get('BACKPROP_REFERENCE_PYTHON'):
            try:
                importlib.metadata.distribution('wandb')
            except importlib.metadata.PackageNotFoundError:
                self.skipTest('Pinned W&B0.30.0 wheel is required')
        reference = reference_runtime()
        observations = []
        for sdk in ('reference', 'native'):
            output = ROOT / f'test-results/public-api-constructor-{scenario}-{sdk}.json'
            environment = {key: value for key, value in os.environ.items()
                           if not key.startswith(('WANDB_', 'BACKPROP_', 'FOUNDRY_'))}
            environment.update(PYTHONDONTWRITEBYTECODE='1',
                               PYTHONPATH=str(ROOT/'tests') if sdk == 'reference' else str(ROOT/'sdk'))
            result = run_observation([reference.python if sdk == 'reference' else sys.executable, '-B', str(ROOT/'tests/public_api_constructor_probe.py'), sdk, scenario],
                                     output, cwd=tempfile.gettempdir(), env=environment, timeout=40)
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
            observations.append(json.loads(output.read_text())['observations'])
        return observations

    def test_settings_timeouts_proxy_and_verification_match(self):
        for scenario in ('defaults', 'setup', 'environment', 'overrides', 'timeout', 'env_timeout', 'proxy', 'env_proxy', 'explicit'):
            with self.subTest(scenario=scenario):
                reference, native = self.observe(scenario)
                self.assertNotIn('error', reference)
                self.assertEqual(native, reference)
                self.assertEqual(native['trace'][-1], {'verify': True})

    def test_override_retention_across_imports_and_unused_proxy_keys_match(self):
        for scenario in ('retained_root', 'retained_public', 'retained_module',
                         'iterable_overrides', 'extra_proxy_keys'):
            with self.subTest(scenario=scenario):
                reference, native = self.observe(scenario)
                self.assertNotIn('error', reference)
                self.assertEqual(native, reference)
                self.assertEqual(native['trace'][-1], {'verify': True})
                if scenario != 'extra_proxy_keys':
                    self.assertTrue(native['nested_identity'])
                    self.assertTrue(native['caller_edit_visible'])
                    self.assertEqual(native['global_defaults'], {'entity': None, 'project': None})
                else:
                    self.assertEqual(native['retained']['_proxies']['unused'], 'retained-only')
                    self.assertEqual(native['trace'][0]['http_proxy'], 'http://proxy.invalid:8080')
                    self.assertIsNone(native['trace'][0]['https_proxy'])

    def test_malformed_override_mappings_match_before_transport_construction(self):
        for scenario in ('invalid_overrides_integer', 'invalid_overrides_string', 'invalid_overrides_pair'):
            with self.subTest(scenario=scenario):
                reference, native = self.observe(scenario)
                self.assertIn(reference['error'], {'TypeError', 'ValueError'})
                self.assertEqual(native, reference)
                self.assertEqual(native['trace'], [])

    def test_api_proxy_overrides_do_not_replace_process_defaults(self):
        reference, native = self.observe('proxy_scope')
        self.assertNotIn('error', reference)
        self.assertEqual(native, reference)
        self.assertEqual(native['global_proxies'],
                         {'http_proxy': 'http://original.invalid:8080', 'https_proxy': None})
        self.assertEqual(native['trace'][0]['https_proxy'], 'http://proxy.invalid:8090')

    def test_api_late_environment_defaults_do_not_mutate_retained_setup(self):
        reference, native = self.observe('late_environment_scope')
        self.assertNotIn('error', reference)
        self.assertEqual(native, reference)
        self.assertEqual(native['settings']['project'], 'late-project')
        self.assertIsNone(native['retained_setup_project'])
        self.assertIsNone(native['after_environment_removed'])

    def test_verification_failure_is_explicit_authentication_error(self):
        reference, native = self.observe('verify_failure')
        self.assertEqual(reference['error'], 'AuthenticationError')
        self.assertEqual(native.get('error'), reference['error'])
        self.assertEqual(native['trace'], reference['trace'])
        # Provider error wording is deliberately different and retained above.
        self.assertNotIn('a' * 40, native['message'])

    def test_published_session_precedes_later_environment_and_explicit_key_wins(self):
        for scenario, selected in (('session', 'session'), ('session_explicit', 'explicit')):
            with self.subTest(scenario=scenario):
                reference, native = self.observe(scenario)
                self.assertNotIn('error', reference)
                self.assertEqual(reference['token_source'], selected)
                self.assertEqual(native, reference)
                self.assertEqual(native['trace'][0]['token_source'], selected)
                self.assertEqual(native['trace'][-1], {'verify': True})

    def test_foreign_host_uses_its_environment_key_instead_of_prior_session(self):
        reference, native = self.observe('session_foreign_environment')
        self.assertNotIn('error', reference)
        self.assertEqual(native, reference)
        self.assertEqual(native['token_source'], 'environment')
        self.assertEqual(native['trace'][0]['host'], 'https://foreign-public-api.invalid')

    def test_successful_implicit_api_publishes_session_and_explicit_api_preserves_it(self):
        for scenario, expected in (('api_session', ['session', 'session']),
                                   ('api_session_explicit', ['session', 'explicit', 'session'])):
            with self.subTest(scenario=scenario):
                reference, native = self.observe(scenario)
                self.assertNotIn('error', reference)
                self.assertEqual(native, reference)
                self.assertEqual(native['token_source'], 'session')
                self.assertEqual([item['token_source'] for item in native['trace'] if 'token_source' in item], expected)
                self.assertEqual(len([item for item in native['trace'] if item.get('verify')]), len(expected))

    def test_foreign_host_without_credentials_rejects_pinned_host_fallback(self):
        reference, native = self.observe('session_foreign_empty')
        # Pinned _use_system_auth returns its old global auth when no credential
        # exists for the requested host. ServiceApi silently queries that old
        # host although public settings still expose the newly requested host.
        # Native intentionally keeps host ownership and fails before any query.
        self.assertNotIn('error', reference)
        self.assertEqual(reference['settings']['base_url'], 'https://foreign-public-api.invalid')
        self.assertEqual(reference['trace'][0]['host'], 'https://public-api.invalid')
        self.assertEqual(reference['token_source'], 'session')
        self.assertEqual(native.get('error'), 'UsageError')
        self.assertEqual(native['trace'], [])
        for token in ('a' * 40, 'b' * 40, 'c' * 40):
            self.assertNotIn(token, native['message'])


class PublicApiConstructionFailures(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.settings = backprop.Settings(credentials_file=str(Path(self.directory.name)/'credentials.json'))
        self.config = {'host': 'https://public-api.invalid', 'stream_dataset_rid': 'ri.foundry.main.dataset.fixture'}
        self.public = importlib.import_module('backprop.apis.public.api')
        self.runtime = importlib.import_module('backprop.run')
        self.session = importlib.import_module('backprop.login_session')
        self.addCleanup(patch.stopall)
        patch.object(self.session, '_init_session', None).start()
        patch.dict(os.environ, {}, clear=True).start()
        patch.object(self.public, 'load_config', return_value=self.config).start()
        patch.object(self.runtime, 'resolve_settings', return_value=self.settings).start()
        self.factory = patch.object(self.public, 'native_store').start()
        self.persist = patch('backprop.login_session.persist_candidate').start()
        self.prompt = patch('backprop.login_prompt.prompt_token', return_value='fixture-token').start()

    def test_verification_failure_does_not_persist_prompt_or_mutate_configuration(self):
        previous = SimpleNamespace(token='existing-other-host-token', host='https://other-public-api.invalid', mode='online')
        self.session._init_session = previous
        failure = ConnectionError('fixture rejection')
        self.factory.return_value.verify_credentials.side_effect = failure
        with self.assertRaises(backprop.AuthenticationError) as caught:
            backprop.Api()
        self.assertIs(caught.exception.__cause__, failure)
        self.assertEqual(self.config, {'host': 'https://public-api.invalid', 'stream_dataset_rid': 'ri.foundry.main.dataset.fixture'})
        self.assertEqual(dict(os.environ), {})
        self.persist.assert_not_called()
        self.prompt.assert_called_once()
        self.assertIs(self.session._init_session, previous)

    def test_successful_prompt_is_persisted_after_read_verification(self):
        order = []
        self.factory.return_value.verify_credentials.side_effect = lambda: order.append('verified')
        self.persist.side_effect = lambda *args: order.append('persisted')
        api = backprop.Api()
        self.assertEqual(order, ['verified', 'persisted'])
        self.assertEqual(api.api_key, 'fixture-token')
        self.assertEqual(api._cache, {})
        self.assertEqual(self.session.current_init_session('https://public-api.invalid').token, 'fixture-token')

    def test_verifier_timeout_cannot_publish_credentials_after_late_return(self):
        previous = SimpleNamespace(token='existing-other-host-token', host='https://other-public-api.invalid', mode='online')
        self.session._init_session = previous
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        def verify():
            entered.set()
            release.wait(2)
            finished.set()
        self.factory.return_value.verify_credentials.side_effect = verify
        started = time.monotonic()
        try:
            with self.assertRaises(backprop.AuthenticationError) as caught:
                backprop.Api(timeout=.02)
            self.assertIsInstance(caught.exception.__cause__, TimeoutError)
            self.assertTrue(entered.is_set())
            self.assertLess(time.monotonic()-started, .5)
            self.persist.assert_not_called()
            self.assertIs(self.session._init_session, previous)
        finally:
            release.set()
            self.assertTrue(finished.wait(2))
        self.persist.assert_not_called()
        self.assertIs(self.session._init_session, previous)
        self.assertEqual(dict(os.environ), {})

    def test_invalid_transport_and_malformed_overrides_fail_before_verification(self):
        for timeout in (float('inf'), float('nan'), '19'):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                backprop.Api(timeout=timeout, api_key='fixture-token')
        for overrides in (17, 'invalid', [('project',)]):
            with self.subTest(overrides=overrides), self.assertRaises((TypeError, ValueError)):
                backprop.Api(overrides=overrides, api_key='fixture-token')
        self.factory.assert_not_called()
        self.persist.assert_not_called()

    def test_nonpositive_deadline_rejects_without_starting_a_verification_worker(self):
        for timeout in (0, -1, False):
            with self.subTest(timeout=timeout), self.assertRaises(backprop.AuthenticationError) as caught:
                backprop.Api(timeout=timeout, api_key='fixture-token')
            self.assertIsInstance(caught.exception.__cause__, TimeoutError)
        self.factory.return_value.verify_credentials.assert_not_called()
        self.persist.assert_not_called()
        self.assertIsNone(self.session._init_session)

    def test_retained_organization_does_not_enable_excluded_services(self):
        api = backprop.Api(overrides={'organization': 'retained-default', 'custom_metadata': {'key': 'value'}},
                           api_key='fixture-token')
        self.assertEqual(api.settings['organization'], 'retained-default')
        self.assertEqual(api.settings['custom_metadata'], {'key': 'value'})
        before = self.factory.return_value.mock_calls.copy()
        for name in ('organization', 'registry', 'registries', 'create_team', 'create_user'):
            with self.subTest(name=name), self.assertRaises(backprop.UnsupportedFeatureError) as caught:
                getattr(api, name)('requested-operation')
            self.assertIsInstance(caught.exception, NotImplementedError)
            self.assertEqual(self.factory.return_value.mock_calls, before)
        self.persist.assert_not_called()


if __name__ == '__main__':
    unittest.main()
