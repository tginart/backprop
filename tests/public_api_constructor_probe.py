"""Observe real pinned/native Api construction at a declared service boundary.

Architecture: isolated SDK process -> real settings/auth/constructor -> supplied
remote verifier -> exact constructor observations and error class. Main caller:
test_public_api_constructor_contracts. This does not contact W&B or Foundry;
all keys and origins below are fixed nonsecret fixtures.
"""
from contextlib import ExitStack
import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

REFERENCE, SCENARIO, OUTPUT = sys.argv[1] == 'reference', sys.argv[2], Path(sys.argv[3])
HOST = 'https://public-api.invalid'
FOREIGN_HOST = 'https://foreign-public-api.invalid'
KEY = 'a' * 40
ENVIRONMENT_KEY = 'b' * 40
EXPLICIT_KEY = 'c' * 40
LOGIN_SCENARIOS = {'session', 'session_explicit', 'session_foreign_environment', 'session_foreign_empty'}
API_SESSION_SCENARIOS = {'api_session', 'api_session_explicit'}
SESSION_SCENARIOS = LOGIN_SCENARIOS | API_SESSION_SCENARIOS


def token_source(token):
    """Label fixed fixture tokens without placing credentials in observations.

    Main callers: the service boundary and completed constructor observation.
    Distinct labels prove precedence; a shared token cannot expose that defect.
    """
    return {KEY: 'session', ENVIRONMENT_KEY: 'environment', EXPLICIT_KEY: 'explicit'}.get(token, 'unknown')


with tempfile.TemporaryDirectory() as directory:
    os.environ.update(HOME=directory, WANDB_CONFIG_DIR=directory, BACKPROP_DIR=directory,
                      WANDB_BASE_URL=HOST, WANDB_API_KEY=KEY, BACKPROP_TOKEN=KEY, WANDB_MODE='offline',
                      BACKPROP_HOST=HOST, BACKPROP_STREAM_DATASET_RID='ri.foundry.main.dataset.fixture')
    if SCENARIO in SESSION_SCENARIOS: os.environ['WANDB_MODE'] = 'online'
    if SCENARIO == 'env_timeout': os.environ['WANDB_HTTP_TIMEOUT'] = '31'
    sdk = importlib.import_module('wandb' if REFERENCE else 'backprop')
    if REFERENCE: assert sdk.__version__ == '0.30.0'
    public = importlib.import_module(('wandb' if REFERENCE else 'backprop') + '.apis.public.api')
    trace = []
    setup = {'base_url': HOST, 'silent': True, 'console': 'off', 'disable_git': True,
             'x_disable_stats': True, 'credentials_file': str(Path(directory)/'credentials.json')}
    if SCENARIO in {'setup', 'environment'}:
        setup.update(entity='setup-owner', project='setup-project')
    if SCENARIO == 'proxy_scope':
        setup['http_proxy'] = 'http://original.invalid:8080'
    setup_owner = sdk.setup(sdk.Settings(**setup))
    if SCENARIO in LOGIN_SCENARIOS:
        # Public login publishes the real SDK session and isolated netrc entry.
        # Change later environment credentials only after that stage completes.
        assert sdk.login(key=KEY, host=HOST, verify=False) is True
        os.environ.update(WANDB_API_KEY=ENVIRONMENT_KEY, BACKPROP_TOKEN=ENVIRONMENT_KEY)
        if SCENARIO == 'session_foreign_empty':
            for name in ('WANDB_API_KEY', 'BACKPROP_TOKEN', 'FOUNDRY_TOKEN'):
                os.environ.pop(name, None)
    # W&B0.30.0's Settings loader rejects this JSON variable at setup time;
    # the legacy Api parser itself accepts it after setup. Exercise that exact
    # constructor branch without claiming the upstream startup bug is absent.
    if SCENARIO == 'env_proxy': os.environ['WANDB__PROXIES'] = '{"https":"http://proxy.invalid:8090"}' 
    if SCENARIO == 'environment':
        os.environ.update(WANDB_ENTITY='environment-owner', WANDB_PROJECT='environment-project')
    if SCENARIO == 'late_environment_scope':
        os.environ['WANDB_PROJECT'] = 'late-project'
    kwargs = {}
    if SCENARIO == 'overrides': kwargs['overrides'] = {'base_url': HOST+'///', 'entity': 'override-owner', 'project': 'override-project', 'run': 'selected'}
    if SCENARIO == 'timeout': kwargs['timeout'] = 2.5
    if SCENARIO in {'proxy', 'proxy_scope'}: kwargs['overrides'] = {'_proxies': {'http': 'http://proxy.invalid:8080', 'https': 'http://proxy.invalid:8090'}}
    if SCENARIO.startswith('retained_'):
        kwargs['overrides'] = {'entity': 'selected-owner', 'project': 'selected-project',
                               'organization': 'retained-organization',
                               'custom_metadata': {'labels': ['initial']}}
    if SCENARIO == 'iterable_overrides':
        kwargs['overrides'] = [('entity', 'selected-owner'), ('project', 'selected-project'),
                               ('custom_metadata', {'labels': ['initial']})]
    if SCENARIO == 'extra_proxy_keys':
        kwargs['overrides'] = {'_proxies': {'http': 'http://proxy.invalid:8080',
                                           'https': None, 'unused': 'retained-only'}}
    if SCENARIO == 'invalid_overrides_integer': kwargs['overrides'] = 17
    if SCENARIO == 'invalid_overrides_string': kwargs['overrides'] = 'invalid'
    if SCENARIO == 'invalid_overrides_pair': kwargs['overrides'] = [('project',)]
    if SCENARIO in {'explicit', 'verify_failure'}: kwargs['api_key'] = KEY
    if SCENARIO == 'session_explicit': kwargs['api_key'] = EXPLICIT_KEY
    if SCENARIO.startswith('session_foreign_'): kwargs['overrides'] = {'base_url': FOREIGN_HOST}
    def verify():
        trace.append({'verify': True})
        if SCENARIO == 'verify_failure': raise ConnectionError('fixture service unavailable')
        return True
    if REFERENCE:
        class Service:
            def __init__(self, *, settings, timeout):
                trace.append({'host': settings.base_url, 'timeout': timeout,
                              'http_proxy': settings.http_proxy, 'https_proxy': settings.https_proxy,
                              'token_matches': settings.api_key == KEY})
                if SCENARIO in SESSION_SCENARIOS: trace[-1]['token_source'] = token_source(settings.api_key)
            def authenticate(self): return verify()
    else:
        class Service:
            def __init__(self, config, token=None):
                self.config, self.token = config, token
                trace.append({'host': config['host'].rstrip('/'), 'timeout': config.get('timeout'),
                              'http_proxy': config.get('http_proxy'), 'https_proxy': config.get('https_proxy'),
                              'token_matches': token == KEY})
                if SCENARIO in SESSION_SCENARIOS: trace[-1]['token_source'] = token_source(token)
            def verify_credentials(self): return verify()
    with ExitStack() as stack:
        stack.enter_context(patch.object(public, 'ServiceApi' if REFERENCE else 'FoundryStore', Service))
        try:
            if SCENARIO in API_SESSION_SCENARIOS:
                # A successful implicit Api publishes its initial environment
                # selection. An explicit Api authenticates only that handle,
                # leaving the selected session for subsequent implicit calls.
                public.Api()
                os.environ.update(WANDB_API_KEY=ENVIRONMENT_KEY, BACKPROP_TOKEN=ENVIRONMENT_KEY)
                if SCENARIO == 'api_session_explicit': public.Api(api_key=EXPLICIT_KEY)
            constructor = (sdk.Api if SCENARIO == 'retained_root' else
                           importlib.import_module(sdk.__name__ + '.apis.public').Api
                           if SCENARIO == 'retained_public' else public.Api)
            api = constructor(**kwargs)
            observation = {'settings': {key: api.settings.get(key) for key in ('base_url', 'entity', 'project', 'run')},
                           'token_matches': api.api_key == KEY, 'trace': trace, 'result': None}
            if SCENARIO.startswith('retained_') or SCENARIO in {'iterable_overrides', 'extra_proxy_keys'}:
                supplied = dict(kwargs['overrides'])
                observation['retained'] = {key: api.settings[key] for key in supplied}
                if 'custom_metadata' in supplied:
                    observation['nested_identity'] = api.settings['custom_metadata'] is supplied['custom_metadata']
                    supplied['custom_metadata']['labels'].append('caller-edit')
                    observation['caller_edit_visible'] = api.settings['custom_metadata']['labels'] == ['initial', 'caller-edit']
                    observation['global_defaults'] = {'entity': sdk.setup().settings.entity,
                                                      'project': sdk.setup().settings.project}
            if SCENARIO in SESSION_SCENARIOS: observation['token_source'] = token_source(api.api_key)
            if SCENARIO == 'proxy_scope':
                observation['global_proxies'] = {key: getattr(sdk.setup().settings, key)
                                                 for key in ('http_proxy', 'https_proxy')}
            if SCENARIO == 'late_environment_scope':
                observation['retained_setup_project'] = setup_owner.settings.project
                os.environ.pop('WANDB_PROJECT')
                observation['after_environment_removed'] = sdk.setup().settings.project
        except Exception as error:
            observation = {'trace': trace, 'error': type(error).__name__, 'message': str(error)}
    sdk.teardown()
OUTPUT.write_text(json.dumps({'sdk': 'reference' if REFERENCE else 'native', 'scenario': SCENARIO, 'observations': observation}, indent=2))
