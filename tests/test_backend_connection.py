"""
Purpose: Verify one backend configuration contract across SDK entry points.
Input: Shared SDK/browser connection fixtures and temporary local storage.
Output: Adapter selection, credential isolation, and architecture boundary checks.
"""
import ast
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import URLError

import backprop
from backprop_backend.connection import create_store, load_config, restore_store, snapshot_store
from backprop_backend.foundry.store import FoundryStore
from backprop_backend.http_store import HttpStore
from backprop_backend.local.server import running_server
from backprop_backend.local.store import LocalStore


ROOT = Path(__file__).resolve().parents[1]
CONNECTIONS = json.loads((ROOT / 'tests/fixtures/backend_connections.json').read_text())


class BackendConnectionTests(unittest.TestCase):
    """
    Purpose: Keep backend decisions out of SDK and dashboard consumers.
    Input: Both connection configurations and the real adapter boundary.
    Output: Compatible selection and separate credential behavior.
    """

    def test_foundry_and_legacy_configs_select_the_same_adapter(self) -> None:
        for explicit in (True, False):
            config = dict(CONNECTIONS['foundry'])
            if not explicit:
                config.pop('backend')
            store = create_store(config, token='fixture-token')
            self.assertIsInstance(store, FoundryStore)
            self.assertIsInstance(store, HttpStore)
            restored = restore_store(snapshot_store(store))
            self.assertEqual(restored.config, store.config)
            self.assertEqual(restored.token, 'fixture-token')

    def test_local_config_connects_sdk_entry_points_without_credentials(self) -> None:
        """
        Purpose: Exercise login, init, Api, and process transfer with one config file.
        Input: One real local server and stale Foundry credentials in the environment.
        Output: Local data without token transmission, prompts, or credential cache access.
        """
        with tempfile.TemporaryDirectory() as directory, running_server(directory) as server:
            with patch.dict('os.environ', {
                'BACKPROP_CONFIG': str(Path(directory) / 'connection.json'),
                'BACKPROP_TOKEN': 'stale-fixture', 'FOUNDRY_TOKEN': 'stale-fixture',
            }), patch('backprop.login_prompt.prompt_token', side_effect=AssertionError('No prompt')), \
                    patch('backprop.credential_store.read_token', side_effect=AssertionError('No credential read')), \
                    patch.object(backprop, '_setup_settings', None):
                self.assertTrue(backprop.login())
                store = create_store(load_config())
                self.assertIsInstance(store, LocalStore)
                self.assertIsInstance(store, HttpStore)
                self.assertNotIsInstance(store, FoundryStore)
                self.assertIsNone(store.token)
                self.assertEqual(restore_store(snapshot_store(store)).ack_namespace, store.ack_namespace)
                with backprop.init(project='selection', id='one-config', dir=directory,
                                   settings={'console': 'off', 'silent': True, 'x_disable_stats': True}) as run:
                    run.log({'loss': 0.25})
                api = backprop.Api()
                self.assertIsNone(api.api_key)
                self.assertEqual(api.run('selection/one-config').history(pandas=False)[0]['loss'], 0.25)

    def test_unknown_backend_is_rejected_before_credentials(self) -> None:
        with self.assertRaisesRegex(ValueError, 'Unknown Backprop backend'):
            create_store({**CONNECTIONS['foundry'], 'backend': 'other'}, token='fixture')

    def test_local_handshake_has_no_independent_retry_loop(self) -> None:
        with patch('urllib.request.OpenerDirector.open', side_effect=URLError(TimeoutError())) as opened, \
                patch('backprop_backend.http_store.time.sleep') as sleep:
            with self.assertRaises(backprop.CommError):
                create_store(CONNECTIONS['local'])
        self.assertEqual(opened.call_count, 1)
        sleep.assert_not_called()

    def test_local_entry_point_deadlines_include_store_construction(self) -> None:
        """
        Purpose: Bound a stalled local handshake before any SDK state is published.
        Input: Login, Api, and init with a short deadline and a blocked handshake.
        Output: Each call expires on time, and late completion leaves no active run.
        """
        with tempfile.TemporaryDirectory() as directory, running_server(directory) as server:
            settings = {'console': 'off', 'silent': True, 'disable_git': True,
                        'x_disable_stats': True, 'init_timeout': 0.05}
            calls = [
                ('login', lambda: backprop.login(timeout=0.05), TimeoutError),
                ('Api', lambda: backprop.Api(timeout=0.05), backprop.AuthenticationError),
                ('init', lambda: backprop.init(project='deadline', dir=directory,
                                               settings=settings), backprop.CommError),
            ]
            for name, call, error in calls:
                with self.subTest(entry_point=name):
                    release, finished = threading.Event(), threading.Event()
                    original = LocalStore.request

                    def blocked(store, path, *args, **kwargs):
                        if path != '/config.json':
                            return original(store, path, *args, **kwargs)
                        try:
                            release.wait(2)
                            return original(store, path, *args, **kwargs)
                        finally:
                            finished.set()

                    with patch.dict('os.environ', {'BACKPROP_CONFIG': str(Path(directory) / 'connection.json')}, clear=True), \
                            patch.object(backprop, '_setup_settings', None), \
                            patch.object(backprop, 'run', None), \
                            patch.object(LocalStore, 'request', blocked):
                        start = time.monotonic()
                        try:
                            with self.assertRaises(error):
                                call()
                            self.assertLess(time.monotonic() - start, 0.5)
                        finally:
                            release.set()
                            self.assertTrue(finished.wait(2))
                        self.assertIsNone(backprop.run)
                        self.assertIsNone(backprop._setup_settings)

    def test_shared_connection_fixtures_load_without_shape_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict('os.environ', {}, clear=True):
            for name, config in CONNECTIONS.items():
                path = Path(directory) / f'{name}.json'
                path.write_text(json.dumps(config))
                self.assertEqual(load_config(path), config)

    def test_consumers_do_not_import_or_select_specific_backends(self) -> None:
        """
        Purpose: Prevent backend decisions from returning to SDK and dashboard consumers.
        Input: The consumer modules that previously selected a backend.
        Output: No backend-specific imports, selector branches, or HTTP endpoint code.
        """
        for name in ('transport.py', '__init__.py', 'run.py', 'run_handoff.py',
                     'control_store.py', 'apis/public/api.py'):
            tree = ast.parse((ROOT / 'sdk/backprop' / name).read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    self.assertNotIn(node.module, {'foundry', 'local_store', 'local_config',
                                                  'login_session', 'credential_store'})
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    if node.func.attr == 'get' and node.args:
                        self.assertFalse(isinstance(node.args[0], ast.Constant)
                                         and node.args[0].value == 'backend', name)
        self.assertFalse(any((ROOT / 'backend').rglob('*.js')))
        for name in ('api.js', 'auth.js', 'app.mjs'):
            source = (ROOT / 'web' / name).read_text()
            self.assertNotIn('/api/v2/', source)
            self.assertNotIn('/multipass/', source)
            self.assertNotIn('FOUNDRY_TOKEN', source)
            self.assertNotIn('applicationConfig?.backend', source)


if __name__ == '__main__':
    unittest.main()
