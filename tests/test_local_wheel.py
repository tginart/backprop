"""
Purpose: Verify an installed wheel without an import from the source checkout.
Input: BACKPROP_TEST_WHEEL and its optional adjacent source distribution.
Output: Assertions for installed assets, SDK calls, and an abrupt server restart.
Note: The test installs only into a temporary directory and uses no network.
"""
import json
import os
from pathlib import Path
import selectors
import subprocess
import sys
import tarfile
import tempfile
import unittest
from urllib.request import urlopen
import zipfile

import pydantic


@unittest.skipUnless(os.environ.get("BACKPROP_TEST_WHEEL"), "Set BACKPROP_TEST_WHEEL to a built wheel")
class LocalWheelTests(unittest.TestCase):
    """
    Purpose: Check the distributable CLI with an isolated package import path.
    Input: One built Backprop wheel and existing runtime dependencies.
    Output: A working dashboard and durable SDK data outside the checkout.
    """

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.wheel = Path(os.environ["BACKPROP_TEST_WHEEL"]).resolve()
        self.installed = self.root / "installed"
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "--no-deps", "--no-compile",
             "--target", str(self.installed), str(self.wheel)],
            check=True, capture_output=True, text=True,
        )
        dependency_path = str(Path(pydantic.__file__).resolve().parent.parent)
        self.env = {**os.environ, "PYTHONPATH": os.pathsep.join(
            [str(self.installed), dependency_path]), "PYTHONNOUSERSITE": "1"}
        self.data = self.root / "data"

    def test_wheel_and_sdist_include_only_dashboard_source_assets(self) -> None:
        with zipfile.ZipFile(self.wheel) as wheel:
            names = set(wheel.namelist())
        for name in ("index.html", "app.mjs", "api.js", "auth.js",
                     "plotly-strict-4.1.1.min.js", "styles.css"):
            self.assertIn("backprop_backend/dashboard/" + name, names)
        self.assertNotIn("backprop_backend/dashboard/config.json", names)
        self.assertNotIn("backprop_backend/dashboard/renderer-package-lock.json", names)
        self.assertIn("backprop_backend/dashboard_assets.json", names)
        for module in ("server", "database", "queries", "journal_import"):
            self.assertIn(f"backprop_backend/local/{module}.py", names)
        self.assertNotIn("backprop/local_server.py", names)
        self.assertFalse(any(name.startswith("wandb/") for name in names))
        self.assertIn("backprop_backend/foundry/store.py", names)
        self.assertIn("backprop_backend/dashboard/backends/foundry.js", names)
        self.assertIn("backprop_backend/dashboard/backends/local.js", names)
        self.assertIn("backprop_backend/dashboard/backends/connection.js", names)
        self.assertIn("backprop_backend/dashboard/backends/http.js", names)
        self.assertNotIn("backprop_backend/browser.js", names)
        self.assertNotIn("backprop_backend/foundry/browser.js", names)
        self.assertFalse(any(name.endswith("/state.json") for name in names))
        for archive in self.wheel.parent.glob("*.tar.gz"):
            with tarfile.open(archive) as source:
                names = source.getnames()
            self.assertTrue(any(name.endswith("/web/index.html") for name in names))
            self.assertTrue(any(name.endswith("/setup.py") for name in names))
            self.assertFalse(any(name.endswith("/web/config.json") for name in names))

    def test_installed_cli_and_sdk_survive_process_kill(self) -> None:
        """
        Purpose: Verify installed cli and sdk survive process kill.
        Input: Isolated test data and the real backend interfaces.
        Output: Assertions for the documented storage contract.
        """
        server, origin = self._start_server()
        connection = json.loads(urlopen(origin + "/config.json").read())
        command = """
import pathlib
import backprop
import backprop_backend.local.server as local_server
assert pathlib.Path(local_server.__file__).is_relative_to(pathlib.Path(INSTALLED))
assert pathlib.Path(backprop.__file__).is_relative_to(pathlib.Path(INSTALLED))
assert backprop.login()
with backprop.init(project='wheel', id='installed', dir=RUN_DIRECTORY,
                   settings={'console':'off','disable_git':True,'x_disable_stats':True}) as run:
    run.log({'loss':0.125})
assert backprop.Api().run('wheel/installed').history(pandas=False)[0]['loss'] == 0.125
"""
        command = command.replace("INSTALLED", repr(str(self.installed)))
        command = command.replace("RUN_DIRECTORY", repr(str(self.root / "runs")))
        result = subprocess.run(
            [sys.executable, "-S", "-c", command], cwd=self.root,
            env={**self.env, "BACKPROP_CONFIG": str(self.data / "connection.json")},
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        stream = connection["stream_dataset_rid"]
        path = f"/api/v2/highScale/streams/datasets/{stream}/streams/master/getEndOffsets"
        before = json.loads(urlopen(origin + path).read())
        server.kill()
        server.wait(timeout=10)
        restarted, origin = self._start_server()
        self.assertIsNone(restarted.poll())
        restored = json.loads(urlopen(origin + "/config.json").read())
        self.assertEqual(restored["local_id"], connection["local_id"])
        self.assertEqual(json.loads(urlopen(origin + path).read()), before)
        with urlopen(origin + "/") as response:
            self.assertIn(b"Experiment workspace", response.read())
            self.assertIn("script-src 'self'", response.headers["Content-Security-Policy"])

    def _start_server(self) -> tuple[subprocess.Popen, str]:
        """
        Purpose: Start only the installed package and bound the startup wait.
        Input: The isolated import path and data directory.
        Output: A child process and its loopback origin.
        """
        process = subprocess.Popen(
            [sys.executable, "-S", "-m", "backprop", "serve", str(self.data),
             "--port", "0"], cwd=self.root, env=self.env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )

        def cleanup() -> None:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=10)
            process.stdout.close()
            process.stderr.close()

        self.addCleanup(cleanup)
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            self.assertTrue(selector.select(timeout=10), "Installed server did not start")
        line = process.stdout.readline()
        self.assertTrue(line.startswith("Backprop dashboard: "), line)
        return process, line.split(": ", 1)[1].strip().rstrip("/")


if __name__ == "__main__":
    unittest.main()
