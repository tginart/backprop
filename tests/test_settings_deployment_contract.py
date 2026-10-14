"""Compare deployment labels with the pinned public Settings properties.

Architecture: real isolated W&B/native processes -> construct and reassign URL
settings -> compare normalized URLs and both read-only computed properties.
Main callers: SDK contract CI. These labels describe W&B's default/custom server
choice; they cannot be used to infer where a Foundry installation is hosted.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
PROBE = '''
import json,sys
if sys.argv[1] == 'native':
 from backprop.compatibility import install_wandb_aliases
 install_wandb_aliases()
import wandb
urls = ['https://api.wandb.ai', 'https://api.wandb.ai///',
 'https://api.wandb.com', 'https://api.wandb.ai:443',
 'https://api.wandb.ai/custom', 'http://localhost:8080/',
 'https://foundry.example.com/',
 'https://custom.example.net']
result = []
settings = wandb.Settings()
for url in urls:
 constructed = wandb.Settings(base_url=url)
 settings.base_url = url
 result.append([[item.base_url, item.is_local, item.deployment]
                for item in [constructed, settings]])
print(json.dumps(result))
'''


class SettingsDeploymentContract(unittest.TestCase):
    def test_urls_and_assignment_match_real_pinned_properties(self):
        from importlib.metadata import version, PackageNotFoundError
        try:
            reference_version = version('wandb')
        except PackageNotFoundError:
            self.skipTest('Pinned W&B reference wheel is unavailable')
        self.assertEqual(reference_version, '0.30.0')
        outputs = []
        with tempfile.TemporaryDirectory() as directory:
            for implementation in ('reference', 'native'):
                env = {k: v for k, v in os.environ.items()
                       if not k.startswith(('WANDB_', 'BACKPROP_'))}
                env['PYTHONPATH'] = str(ROOT/'sdk') if implementation == 'native' else ''
                process = subprocess.run([sys.executable, '-B', '-c', PROBE, implementation],
                    cwd=directory, env=env, capture_output=True, text=True, timeout=60)
                self.assertEqual(process.returncode, 0, process.stderr)
                outputs.append(json.loads(process.stdout))
        self.assertEqual(outputs[0], outputs[1])
        self.assertEqual(outputs[1][6][0][1:], [True, 'local'])
