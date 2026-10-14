"""Actual pinned Figure-file parity, tested independently of browser geometry.

Architecture: isolated upstream/local probes -> source/version checks -> every
public import's exact bytes/errors -> binary encoding and trace-family coverage.
Main callers: optional SDK test discovery and the combined parity release pass.
Optional dependencies stay optional; this suite installs no package or runtime.
"""
from __future__ import annotations

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
PINNED_PLOTLY_SOURCE = "d50a5d0c611821910864b43da66e29c38328ee28becbd86beff63d20429785f3"
HAS_OPTIONAL = all(importlib.util.find_spec(name) for name in ["wandb", "plotly", "numpy", "matplotlib"])


@unittest.skipUnless(HAS_OPTIONAL, "Pinned W&B, Plotly, NumPy and matplotlib runtime required")
class PlotlyEncodingGapContracts(unittest.TestCase):
    def test_genuine_trace_matrix_and_binary_inputs_match_at_each_public_import(self):
        reference = importlib.metadata.distribution("wandb")
        self.assertEqual(reference.version, "0.30.0")
        observations = []
        for mode in ["reference", "native"]:
            result = subprocess.run([sys.executable, "-B", str(ROOT / "tests/plotly_gap_reference_probe.py"), mode],
                cwd=tempfile.gettempdir(), timeout=120, text=True, capture_output=True,
                env={**os.environ, "MPLBACKEND": "Agg", "PYTHONPATH": str(reference.locate_file("")) if mode == "reference" else str(ROOT / "sdk")})
            self.assertEqual(result.returncode, 0, result.stderr)
            observations.append(json.loads(result.stdout))
        self.assertEqual(observations[0].pop("source_sha256"), PINNED_PLOTLY_SOURCE)
        self.assertEqual(observations[0]["figure_names"], observations[1]["figure_names"])
        self.assertEqual(len(observations[0]["figure_names"]), 60)
        for alias, expected in observations[0]["aliases"].items():
            with self.subTest(alias=alias):
                actual = observations[1]["aliases"][alias]
                self.assertEqual(actual, expected)
                self.assertTrue(actual["same_class"])
                self.assertTrue(actual["frozen"])
                self.assertEqual(actual["matplotlib_image_error"], "ValueError")
                self.assertEqual(actual["matplotlib_image_media"], "Image")
                for dtype in ["i1", "u1", "i2", "u2", "i4", "u4", "f4", "f8", "i8", "u8"]:
                    spec = json.loads(actual["encoding"]["typed_" + dtype]["bytes"])
                    self.assertEqual(spec["data"][0]["x"]["bdata"], json.loads(expected["encoding"]["typed_" + dtype]["bytes"])["data"][0]["x"]["bdata"])
                    self.assertIn(spec["data"][0]["x"]["dtype"], {"i1", "u1", "i2", "u2", "i4", "u4", "f4", "f8"})
                matrix = json.loads(actual["encoding"]["typed_matrix"]["bytes"])
                self.assertEqual(matrix["data"][0]["z"]["shape"], "2, 2")
                self.assertEqual(json.loads(actual["encoding"]["animation"]["bytes"])["frames"][0]["traces"], [0])
        if os.environ.get("PLOTLY_GAP_OUTPUT"):
            Path(os.environ["PLOTLY_GAP_OUTPUT"]).write_text(json.dumps({"reference": observations[0], "native": observations[1]}, sort_keys=True))


if __name__ == "__main__":
    unittest.main()
