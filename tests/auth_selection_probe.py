"""Run native or pinned authentication selection in an independent process.

Architecture: hash-verified W&B selection body or native selector -> identical
credential-source/prompt fixtures -> JSON outcomes and forwarding arguments.
Only hosted identity and terminal I/O are supplied as fixtures. Main caller:
test_sdk_credentials; the two modes never share session globals or environments.
"""
import json
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

import backprop
from backprop.login_session import authenticate_candidate
from test_sdk_login_session import source_function, AUTH


class HostUrl:
    def __init__(self, value): self.url = value


def compare(kind):
    outcomes = []
    with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'FOUNDRY_TOKEN': 'reused-fixture'}, clear=True):
        for setting in [False, True]:
            for override in [None, False, True]:
                for allow_prompt in [False, True]:
                    for force in [False, True]:
                        calls = []
                        if kind == 'reference':
                            def prompted(**kwargs):
                                calls.append({'force': kwargs['no_offline'], 'timeout': kwargs['input_timeout'], 'referrer': kwargs['referrer']})
                                assert kwargs['no_create'] == kwargs['no_offline']
                                return 'prompted-fixture'
                            reference = source_function(AUTH, 'authenticate_session', {
                                'HostUrl': HostUrl, 'session_credentials': lambda **kwargs: None,
                                '_use_system_auth': lambda **kwargs: 'reused-fixture', '_use_prompted_auth': prompted})
                            result = reference(host='https://credentials.invalid', source='probe',
                                relogin=setting if override is None else override, prompt=allow_prompt,
                                no_offline=force, no_create=force, input_timeout=3, referrer='fixture')
                        else:
                            def prompted(host, **kwargs):
                                calls.append(kwargs); return 'prompted-fixture'
                            selected = backprop.Settings(relogin=setting, credentials_file=str(Path(directory)/'credentials.json'))
                            with patch('backprop.login_prompt.prompt_token', side_effect=prompted):
                                result = authenticate_candidate(key=None, host='https://credentials.invalid',
                                    prompt=allow_prompt, verify=False, timeout=3, relogin=override, force=force,
                                    referrer='fixture', settings=selected, load_config=lambda: {}, native_store=None)
                            result = result.token if result else None
                        outcomes.append({'settings_relogin':setting, 'relogin':override, 'prompt':allow_prompt,
                                         'force':force, 'result':result, 'prompt_calls':calls})
    return outcomes


if __name__ == '__main__': print(json.dumps(compare(sys.argv[1])))
