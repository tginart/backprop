"""
Purpose: Run repeatable source, package, browser, and optional live Foundry verification.
Input: An output directory, Python/Node runtimes, and optional Foundry connection credentials.
Output: Phase logs, acceptance reports, distribution artifacts, and a JSON summary.
Note: Real Foundry phases use unique test projects and do not provision resources.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import time
import zipfile


ROOT = Path(__file__).resolve().parents[1]


def verify(args: argparse.Namespace) -> dict:
    """
    Purpose: Execute the verification matrix with isolated package build outputs.
    Input: Parsed options and the current source checkout.
    Output: A summary with explicit passed, failed, and unrequested live phases.
    """
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    environment = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1',
                   'PYTHONPATH': str(ROOT / 'sdk'), 'BACKPROP_PYTHON': sys.executable}
    report = {'started_at': datetime.now(timezone.utc).isoformat(), 'phases': [],
              'status': 'running', 'live_foundry_requested': bool(args.foundry_config),
              'live_gcp_requested': bool(args.gcp_config),
              'oauth_note': 'Foundry and Google consent use protocol fixtures; browser data can use real backend HTTP.'}
    fingerprint = hashlib.sha256()
    for folder in ('backend', 'sdk', 'web', 'tests', 'tools'):
        for path in sorted((ROOT / folder).rglob('*')):
            if path.is_file() and path.suffix in {'.py', '.js', '.mjs'} and '__pycache__' not in path.parts:
                fingerprint.update(str(path.relative_to(ROOT)).encode())
                fingerprint.update(path.read_bytes())
    report['source_sha256'] = fingerprint.hexdigest()
    report['python_version'] = sys.version.split()[0]
    report['node_version'] = subprocess.check_output(['node', '--version'], text=True).strip()
    summary = output / 'summary.json'
    summary.write_text(json.dumps(report, indent=2) + '\n')

    def phase(name: str, command: list[str], env: dict | None = None, cwd: Path = ROOT) -> None:
        """
        Purpose: Retain complete phase evidence and fail at the first failed gate.
        Input: One command without credentials in its arguments.
        Output: A phase log and an updated summary file.
        """
        start = time.monotonic()
        log = output / (name + '.log')
        print('RUN: ' + name, flush=True)
        with log.open('w') as stream:
            result = subprocess.run(command, cwd=cwd, env=env or environment,
                                    stdout=stream, stderr=subprocess.STDOUT)
        report['phases'].append({'name': name, 'status': 'passed' if result.returncode == 0 else 'failed',
                                 'exit_code': result.returncode, 'seconds': round(time.monotonic() - start, 2),
                                 'log': str(log)})
        summary.write_text(json.dumps(report, indent=2) + '\n')
        if result.returncode:
            raise RuntimeError(f'{name} failed; inspect {log}')
        print('PASS: ' + name, flush=True)

    phase('python-contracts', [sys.executable, '-B', '-m', 'unittest', 'discover', '-s', 'tests', '-p', 'test_*.py', '-q'])
    phase('javascript-contracts', ['node', '--test', *[str(path) for path in sorted((ROOT / 'tests').glob('test_*.mjs'))]])
    distributions = output / 'dist'
    metadata = output / 'metadata'
    metadata.mkdir(exist_ok=True)
    phase('build-distributions', [sys.executable, 'setup.py', 'egg_info', '--egg-base', str(metadata),
                                 'build', '--build-base', str(output / 'build'),
                                 'sdist', '--dist-dir', str(distributions),
                                 'bdist_wheel', '--dist-dir', str(distributions)])
    wheel = next(distributions.glob('*.whl'))
    installed = output / 'installed'
    phase('install-wheel', [sys.executable, '-m', 'pip', 'install', '--no-deps', '--no-compile',
                            '--target', str(installed), str(wheel)])
    installed_env = {**environment, 'PYTHONPATH': str(installed),
                     'BACKPROP_TEST_PACKAGE_PATH': str(installed), 'BACKPROP_TEST_WHEEL': str(wheel)}
    phase('installed-cli-restart', [sys.executable, '-B', '-m', 'unittest', 'discover', '-s', 'tests', '-p', 'test_local_wheel.py', '-q'], installed_env)
    source = output / 'source'
    source.mkdir(exist_ok=True)
    with tarfile.open(next(distributions.glob('*.tar.gz'))) as archive:
        archive.extractall(source, filter='data')
    source_root = next(path for path in source.iterdir() if path.is_dir())
    rebuilt = output / 'rebuilt'
    phase('rebuild-sdist', [sys.executable, 'setup.py', 'bdist_wheel', '--dist-dir', str(rebuilt)], cwd=source_root)
    with zipfile.ZipFile(wheel) as first, zipfile.ZipFile(next(rebuilt.glob('*.whl'))) as second:
        if set(first.namelist()) != set(second.namelist()):
            raise AssertionError('The source distribution wheel has different files')
        for name in first.namelist():
            if first.read(name) != second.read(name):
                raise AssertionError('The source distribution wheel differs: ' + name)
    phase('local-sdk-acceptance', [sys.executable, '-B', 'tests/backend_acceptance.py', '--backend', 'local',
                                  '--report', str(output / 'local-acceptance.json')], installed_env)
    local_browser = dict(installed_env)
    local_browser.pop('BACKPROP_TEST_FOUNDRY_CONFIG', None)
    phase('local-browser', ['node', 'tests/browser_local_backend_qa.mjs'], local_browser)
    if args.foundry_config:
        config = args.foundry_config.resolve()
        command = [sys.executable, '-B', 'tests/backend_acceptance.py', '--backend', 'foundry',
                   '--foundry-config', str(config), '--archive-timeout', str(args.archive_timeout),
                   '--report', str(output / 'foundry-acceptance.json')]
        if args.deployment_credentials:
            command.append('--deployment-credentials')
        phase('foundry-sdk-acceptance', command, installed_env)
        foundry_browser = {**installed_env, 'BACKPROP_TEST_FOUNDRY_CONFIG': str(config)}
        if args.deployment_credentials:
            foundry_browser['BACKPROP_TEST_DEPLOYMENT_CREDENTIALS'] = '1'
        phase('foundry-browser', ['node', 'tests/browser_local_backend_qa.mjs'], foundry_browser)
    else:
        report['phases'].append({'name': 'real-foundry', 'status': 'not requested'})
    if args.gcp_config:
        config = args.gcp_config.resolve()
        phase('gcp-sdk-acceptance', [sys.executable, '-B', 'tests/backend_acceptance.py',
              '--backend', 'gcp', '--gcp-config', str(config),
              '--report', str(output / 'gcp-acceptance.json')], installed_env)
        phase('gcp-browser', ['node', 'tests/browser_local_backend_qa.mjs'],
              {**installed_env, 'BACKPROP_TEST_GCP_CONFIG': str(config)})
    else:
        report['phases'].append({'name': 'real-gcp', 'status': 'not requested'})
    report['completed_at'] = datetime.now(timezone.utc).isoformat()
    report['status'] = 'passed'
    summary.write_text(json.dumps(report, indent=2) + '\n')
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    parser.add_argument('--output', type=Path, default=ROOT / 'test-results/backend-verification' / stamp,
                        help='Fresh directory for logs, packages, and summary.json')
    parser.add_argument('--foundry-config', type=Path, help='Also verify real Foundry storage with this configuration')
    parser.add_argument('--gcp-config', type=Path, help='Also verify a real GCP deployment using ADC or BACKPROP_GCP_ACCESS_TOKEN')
    parser.add_argument('--deployment-credentials', action='store_true', help='Read the existing deployment credential with gcloud for live Foundry tests')
    parser.add_argument('--archive-timeout', type=float, default=420, help='Maximum seconds for Foundry SQL archive visibility')
    args = parser.parse_args()
    if args.output.exists() and any(args.output.iterdir()):
        parser.error('--output must be empty to prevent stale package or report reuse')
    try:
        verify(args)
    except Exception as error:
        summary = args.output.resolve() / 'summary.json'
        if summary.exists():
            report = json.loads(summary.read_text())
            report.update(status='failed', error_type=type(error).__name__, message=str(error))
            summary.write_text(json.dumps(report, indent=2) + '\n')
        raise
    print('Report: ' + str(args.output.resolve() / 'summary.json'))


if __name__ == '__main__':
    main()
