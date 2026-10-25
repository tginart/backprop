"""
Purpose: Preserve Foundry operator entry points after the backend directory split.
Input: Both current and legacy command paths with no credentials.
Output: Each command exposes its existing arguments from any work directory.
"""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import pydantic


ROOT = Path(__file__).resolve().parents[1]


class BackendLayoutTests(unittest.TestCase):
    """
    Purpose: Check relocated Foundry commands and their compatibility entry points.
    Input: Repository command files and an isolated work directory.
    Output: Help succeeds without an API call or a state-file change.
    """

    def test_source_only_commands_and_assets_without_editable_install(self) -> None:
        """
        Purpose: Keep source commands usable without the editable package finder.
        Input: An isolated interpreter, source SDK path, and existing dependencies.
        Output: Backend assets and documented command help load from the checkout.
        """
        paths = [str(ROOT / "sdk"), str(Path(pydantic.__file__).resolve().parent.parent)]
        bootstrap = "import sys; sys.path[:0] = " + repr(paths) + "; "
        with tempfile.TemporaryDirectory() as directory:
            code = bootstrap + (
                "from backprop_backend.assets import asset_names, asset_path; "
                "assert 'index.html' in asset_names(); "
                "assert asset_path('index.html').is_file()"
            )
            result = subprocess.run([sys.executable, "-S", "-c", code], cwd=directory,
                                    text=True, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            for name in ("backend/verify.py", "tools/verify_public_api.py", "tools/verify_sdk_release.py"):
                # Public exports omit the maintainer-only verification scripts.
                if not (ROOT / name).is_file():
                    continue
                with self.subTest(command=name):
                    code = bootstrap + (
                        "import runpy; sys.argv = [" + repr(name) + ", '--help']; "
                        "runpy.run_path(" + repr(str(ROOT / name)) + ", run_name='__main__')"
                    )
                    result = subprocess.run([sys.executable, "-S", "-c", code], cwd=directory,
                                            text=True, capture_output=True, timeout=10)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn("usage:", result.stdout)

    def test_foundry_commands_preserve_existing_entry_points(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            for folder in ("backend", "backend/foundry"):
                for command in ("provision", "verify"):
                    with self.subTest(folder=folder, command=command):
                        result = subprocess.run(
                            [sys.executable, "-B", str(ROOT / folder / f"{command}.py"),
                             "--help"], cwd=directory, capture_output=True,
                            text=True, timeout=10,
                        )
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertIn("--state", result.stdout)
                        if command == "provision":
                            self.assertIn("--project-rid", result.stdout)
                        else:
                            self.assertIn("--archive-timeout", result.stdout)


if __name__ == "__main__":
    unittest.main()
