"""Optional exact W&B bytes; no rendering assertion can substitute for them.

Architecture: source/version pin -> isolated upstream/local probes -> three
independent alias comparisons. Main caller: focused Python unittest discovery.
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
from plotly_animation_branch_figures import ALL_EASINGS, TIMED_FAMILIES
ROOT = Path(__file__).resolve().parents[1]
OPTIONAL = all(importlib.util.find_spec(name) for name in ["wandb", "plotly", "numpy"])

@unittest.skipUnless(OPTIONAL, "Installed pinned W&B/Plotly/NumPy required")
class AnimationEncoding(unittest.TestCase):
    def test_independent_import_bytes(self):
        reference = importlib.metadata.distribution("wandb")
        self.assertEqual(reference.version, "0.30.0")
        values = []
        for mode in ["reference", "native"]:
            result = subprocess.run([sys.executable, "-B", str(ROOT / "tests/plotly_animation_probe.py"), mode], capture_output=True, text=True, timeout=120, cwd=tempfile.gettempdir(),
                env={**os.environ, "PYTHONPATH": str(reference.locate_file("")) if mode == "reference" else str(ROOT / "sdk")})
            self.assertEqual(result.returncode, 0, result.stderr)
            values.append(json.loads(result.stdout))
        self.assertEqual(values[0].pop("source_sha256"), "d50a5d0c611821910864b43da66e29c38328ee28becbd86beff63d20429785f3")
        self.assertEqual(len(values[0]["aliases"]), 3)
        for name, expected in values[0]["aliases"].items():
            with self.subTest(alias=name):
                self.assertEqual(len(expected), 295)
                self.assertTrue({"interpolate_" + family + "_" + easing for family in TIMED_FAMILIES for easing in ALL_EASINGS} <= set(expected))
                self.assertTrue({"numeric_selection_animation", "branch_scatter_ids", "branch_remapped_traces", "retained_frames_before",
                    "retained_frames_after", "missing_inheritance", "cyclic_inheritance", "branch_quiver_template_null",
                    "branch_quiver_template_frame_type", "quiver_inherited_type_null"} <= set(expected))
                self.assertEqual(expected, values[1]["aliases"][name])
        if os.environ.get("PLOTLY_ANIMATION_ENCODING_OUTPUT"):
            Path(os.environ["PLOTLY_ANIMATION_ENCODING_OUTPUT"]).write_text(json.dumps(values[0], sort_keys=True))

if __name__ == "__main__":
    unittest.main()
