"""Optional installed W&B0.30 exact bytes for each independently invoked alias.

Architecture: isolated native/reference probes -> source pin -> exact saved
bytes. Main caller: focused unittest discovery; browser rendering is separate.
"""
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
OPTIONAL = all(importlib.util.find_spec(name) for name in ["wandb", "plotly", "numpy"])


@unittest.skipUnless(OPTIONAL, "Installed pinned W&B/Plotly/NumPy required")
class LayoutEncoding(unittest.TestCase):
    def test_independent_import_bytes(self):
        reference = importlib.metadata.distribution("wandb")
        self.assertEqual(reference.version, "0.30.0")
        values = []
        for mode in ["reference", "native"]:
            result = subprocess.run([sys.executable, "-B", str(ROOT / "tests/plotly_layout_probe_encoding.py"), mode], capture_output=True, text=True, timeout=60, cwd=tempfile.gettempdir(), env={**os.environ, "PYTHONPATH": str(reference.locate_file("")) if mode == "reference" else str(ROOT / "sdk")})
            self.assertEqual(result.returncode, 0, result.stderr)
            values.append(json.loads(result.stdout))
        self.assertEqual(values[0].pop("source_sha256"), "d50a5d0c611821910864b43da66e29c38328ee28becbd86beff63d20429785f3")
        self.assertEqual(len(values[0]["aliases"]), 3)
        for name, expected in values[0]["aliases"].items():
            with self.subTest(alias=name):
                self.assertEqual(len(expected), 32)
                self.assertEqual(expected, values[1]["aliases"][name])
        self.assertEqual(values[0]["aliases"]["wandb.Plotly"], values[1]["aliases"]["backprop.Plotly"])


if __name__ == "__main__":
    unittest.main()
