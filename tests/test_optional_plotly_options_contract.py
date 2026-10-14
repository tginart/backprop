"""Pin independent file encoding for the concrete Plotly animation/layout gaps.

Architecture: isolated upstream/local probes -> source pin -> every alias and
every options Figure's exact bytes. Main caller: optional SDK test discovery.
Installed optional libraries remain optional; no environment changes are made.
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


@unittest.skipUnless(OPTIONAL, "Pinned W&B and genuine Plotly/NumPy runtime required")
class PlotlyOptionsContract(unittest.TestCase):
    def test_every_alias_matches_pinned_option_and_frame_file_bytes(self):
        reference = importlib.metadata.distribution("wandb")
        self.assertEqual(reference.version, "0.30.0")
        observations = []
        for mode in ["reference", "native"]:
            result = subprocess.run([sys.executable, "-B", str(ROOT / "tests/plotly_gap_options_probe.py"), mode],
                cwd=tempfile.gettempdir(), capture_output=True, text=True, timeout=120,
                env={**os.environ, "PYTHONPATH": str(reference.locate_file("")) if mode == "reference" else str(ROOT / "sdk")})
            self.assertEqual(result.returncode, 0, result.stderr)
            observations.append(json.loads(result.stdout))
        self.assertEqual(observations[0].pop("source_sha256"), "d50a5d0c611821910864b43da66e29c38328ee28becbd86beff63d20429785f3")
        for alias, expected in observations[0]["aliases"].items():
            with self.subTest(alias=alias):
                self.assertEqual(len(expected), 52)
                self.assertEqual(expected, observations[1]["aliases"][alias])
        if os.environ.get("PLOTLY_OPTIONS_ENCODING_OUTPUT"):
            Path(os.environ["PLOTLY_OPTIONS_ENCODING_OUTPUT"]).write_text(json.dumps(observations[0], sort_keys=True))


if __name__ == "__main__":
    unittest.main()
