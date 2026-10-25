"""
Purpose: Run the same SDK acceptance scenario against real local or Foundry storage.
Input: A backend connection, optional deployment credentials, and a bounded archive wait.
Output: A JSON report for isolated test runs, without credentials.
Note: Live tests append only to a unique verification project and immutable test files.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import uuid
from unittest.mock import patch

import backprop
from backprop.transport import BackpropError, Journal
from backprop_backend.connection import create_store
from backprop_backend.local.server import running_server


class LostResponseStore:
    """
    Purpose: Lose one successful publication response at the journal boundary.
    Input: A real backend store.
    Output: One retained acknowledgement followed by a safe identical replay.
    """

    def __init__(self, store):
        self.store = store
        self.did_lose = False

    def __getattr__(self, name):
        return getattr(self.store, name)

    def publish(self, events: list[dict]) -> None:
        self.store.publish(events)
        if not self.did_lose:
            self.did_lose = True
            raise ConnectionError('Acceptance fixture lost the committed response')


def run_scenario(config: dict, *, token: str | None, archive_timeout_s: float) -> dict:
    """
    Purpose: Exercise public SDK calls through the selected real backend adapter.
    Input: One connection and a credential held only in the process environment.
    Output: Passed checks and the unique scope required for traceability.
    """
    identifier = uuid.uuid4().hex[:12]
    project, entity, run_id = 'backprop-verification-' + identifier, 'verification', 'run-' + identifier
    checks = []
    print(f"SCOPE: {config.get('backend', 'foundry')} {entity}/{project}/{run_id}", flush=True)
    with tempfile.TemporaryDirectory(prefix='backprop-acceptance-') as directory, ExitStack() as stack:
        root = Path(directory)
        connection = root / 'connection.json'
        connection.write_text(json.dumps(config))
        environment = {**os.environ, 'BACKPROP_CONFIG': str(connection)}
        if token:
            environment['BACKPROP_TOKEN'] = token
        else:
            environment.update(BACKPROP_TOKEN='unused-foundry-fixture', FOUNDRY_TOKEN='unused-foundry-fixture')
        for name in ('BACKPROP_BACKEND', 'BACKPROP_HOST', 'BACKPROP_STREAM_DATASET_RID',
                     'BACKPROP_MEDIA_DATASET_RID', 'BACKPROP_APP_URL'):
            environment.pop(name, None)
        stack.enter_context(patch.dict(os.environ, environment, clear=True))
        stack.enter_context(patch('backprop.run_stopping.POLL_INTERVAL', 1.0))
        backprop.setup({'credentials_file': str(root / 'unused-credential-cache.json')})
        try:
            assert backprop.login(prompt=False)
            assert not (root / 'unused-credential-cache.json').exists()
            store = create_store(config, token=token)
            stopped = threading.Event()
            settings = {'console': 'off', 'disable_git': True, 'x_disable_stats': True,
                        'silent': True, 'flush_interval': 0.1, 'stop_fn': stopped.set}
            run = backprop.init(project=project, entity=entity, id=run_id, dir=root, settings=settings)
            try:
                run.log({'loss': 1.0}, step=0, commit=True)
                run.log({'loss': 0.5,
                         'table': backprop.Table(columns=['name', 'score'], data=[['acceptance', 3]]),
                         'plotly': backprop.Plotly({'data': [{'type': 'scatter', 'x': [0, 1], 'y': [1, 2]}],
                                                  'layout': {'title': {'text': 'Acceptance'}}})}, step=1, commit=True)
                weights = root / 'weights.txt'
                weights.write_bytes(b'Backprop acceptance weights\n')
                artifact = backprop.Artifact('acceptance-model', type='model')
                artifact.add_file(str(weights))
                run.log_artifact(artifact, aliases=['latest'])
                run.flush()
                api = backprop.Api()
                observed = api.run(f'{entity}/{project}/{run_id}')
                history = observed.history(pandas=False)
                assert [row['loss'] for row in history if 'loss' in row] == [1.0, 0.5]
                rich = next(row for row in history if 'table' in row)
                assert rich['table']['data'] == [['acceptance', 3]]
                assert 'plotly' in rich
                checks.extend(['live metrics', 'history', 'table', 'Plotly'])
                print(config.get('backend', 'foundry') + ': live metrics and rich data passed', flush=True)
                restored = api.artifact(f'{entity}/{project}/acceptance-model:latest')
                downloaded = Path(restored.download(root=str(root / 'download')))
                assert (downloaded / 'weights.txt').read_bytes() == weights.read_bytes()
                checks.append('artifact integrity')
                rows = store.read_events(project=project, run_id=run_id, entity=entity)
                original = next(row for row in rows if row['type'] == 'history')
                retry = Journal(root / 'ambiguous-upload', LostResponseStore(store))
                retry.append(original)
                try:
                    retry.flush()
                except BackpropError:
                    pass
                else:
                    raise AssertionError('The lost response did not retain the journal cursor')
                assert retry.ack == 0
                retry.flush()
                assert retry.ack == 1
                recovered = store.read_events(project=project, run_id=run_id, entity=entity)
                assert sum(row['event_id'] == original['event_id'] for row in recovered) == 1
                checks.append('committed-response loss and duplicate replay')
                key = hashlib.sha256(project.encode()).hexdigest()
                winner = {'project': project, 'choice': 'first'}
                assert store.claim_artifact_resolution(key, winner) == winner
                assert store.claim_artifact_resolution(key, {'choice': 'second'}) == winner
                assert store.read_artifact_resolution(key) == winner
                checks.append('atomic artifact resolution')
                start = next(row for row in recovered if row['type'] == 'run_start')
                instance = start['payload']['control']['instance_id']
                stop = {'schema_version': 1, 'event_id': uuid.uuid4().hex, 'run_id': run_id,
                        'project': project, 'entity': entity, 'type': 'run_stop_request',
                        'timestamp': datetime.now(timezone.utc).isoformat(),
                        'payload': {'action': 'stop', 'instance_id': instance}}
                store.publish([stop, stop])
                assert stopped.wait(60), 'The test run did not receive its own stop request'
                run.flush()
                rows = store.read_events(project=project, run_id=run_id, entity=entity)
                assert sum(row['type'] == 'run_stop_ack' for row in rows) == 1
                checks.append('cooperative stop for the test process')
            finally:
                run.finish()
            resumed = backprop.init(project=project, entity=entity, id=run_id, resume='must',
                                    dir=root, settings={**settings, 'stop_fn': None})
            try:
                assert resumed.resumed
                resumed.log({'loss': 0.25})
            finally:
                resumed.finish()
            history = backprop.Api().run(f'{entity}/{project}/{run_id}').history(pandas=False)
            assert [row['loss'] for row in history if 'loss' in row] == [1.0, 0.5, 0.25]
            checks.append('resume')
            offline = backprop.init(project=project, entity=entity, id='offline-' + identifier,
                                    mode='offline', dir=root, settings=settings)
            offline.log({'offline_metric': 7})
            offline.finish()
            source = Path(offline.sync_dir) / 'events.jsonl'
            before = source.read_bytes()
            backprop.sync(offline.sync_dir)
            assert source.read_bytes() == before
            rows = backprop.Api().run(f'{entity}/{project}/{offline.id}').history(pandas=False)
            assert rows[0]['offline_metric'] == 7
            checks.append('offline journal sync')
            print(config.get('backend', 'foundry') + ': wait for independent SQL archive visibility', flush=True)
            deadline = time.monotonic() + archive_timeout_s
            while True:
                archived = [store.record_to_event(row) for row in
                            store.query_archive(project=project, run_id=run_id, entity=entity)]
                metrics = [row['payload'].get('metrics', {}).get('loss') for row in archived
                           if row['type'] == 'history']
                if {1.0, 0.5, 0.25}.issubset(set(metrics)):
                    break
                if time.monotonic() >= deadline:
                    raise TimeoutError('The backend archive did not contain the test history before its deadline')
                time.sleep(min(10, max(0, deadline - time.monotonic())))
            checks.append('independent SQL archive read')
            return {'backend': config.get('backend', 'foundry'), 'status': 'passed',
                    'project': project, 'run_id': run_id, 'checks': checks}
        except Exception as error:
            error.acceptance_scope = {'backend': config.get('backend', 'foundry'),
                                      'project': project, 'run_id': run_id, 'checks': checks}
            raise
        finally:
            backprop.teardown()


def main() -> None:
    """
    Purpose: Run real acceptance tests for one or both backend implementations.
    Input: CLI configuration, credential source, and report destination.
    Output: A credential-free JSON report and a nonzero exit code on failure.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backend', choices=['local', 'foundry', 'both'], default='local', help='Backend scenario to execute')
    parser.add_argument('--foundry-config', type=Path, help='Existing Foundry connection file; no resources are provisioned')
    parser.add_argument('--deployment-credentials', action='store_true', help='Read the existing deployment secret with gcloud instead of environment credentials')
    parser.add_argument('--archive-timeout', type=float, default=420, help='Maximum seconds for independent SQL archive visibility')
    parser.add_argument('--report', type=Path, help='Optional JSON report destination')
    args = parser.parse_args()
    results = []
    try:
        if args.backend in {'local', 'both'}:
            with tempfile.TemporaryDirectory() as directory, running_server(directory) as server:
                results.append(run_scenario(server.connection, token=None, archive_timeout_s=5))
                print('PASS: real local SDK acceptance', flush=True)
        if args.backend in {'foundry', 'both'}:
            if not args.foundry_config:
                parser.error('--foundry-config is required for real Foundry tests')
            config = json.loads(args.foundry_config.read_text())
            config['backend'] = 'foundry'
            token = os.environ.get('BACKPROP_TOKEN') or os.environ.get('FOUNDRY_TOKEN')
            if args.deployment_credentials:
                result = subprocess.run(['gcloud', 'secrets', 'versions', 'access', 'latest',
                                         '--secret=' + os.environ.get('BACKPROP_TEST_GCLOUD_SECRET', 'FOUNDRY_TOKEN'),
                                         *(['--project=' + os.environ['BACKPROP_TEST_GCLOUD_PROJECT']]
                                           if os.environ.get('BACKPROP_TEST_GCLOUD_PROJECT') else [])],
                                        capture_output=True, text=True, timeout=45, check=True)
                token = result.stdout.strip()
            if not token:
                parser.error('Provide environment credentials or --deployment-credentials')
            results.append(run_scenario(config, token=token, archive_timeout_s=args.archive_timeout))
            print('PASS: real Foundry SDK acceptance', flush=True)
    except Exception as error:
        results.append({'status': 'failed', 'error_type': type(error).__name__, 'message': str(error),
                        **getattr(error, 'acceptance_scope', {})})
        raise
    finally:
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps({'scenarios': results}, indent=2) + '\n')


if __name__ == '__main__':
    main()
