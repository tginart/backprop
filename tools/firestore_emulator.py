"""
Purpose: Run contract tests against Google's checksum-verified Firestore emulator.
Input: Java 21, a private cache directory, and a command after --.
Output: The command's exit status with a temporary loopback emulator.
Note: No Google credentials or cloud resources are used.
"""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import socket
import subprocess
import time
from urllib.request import urlopen


# Source: firebase/firebase-tools src/emulator/downloadableEmulatorInfo.json.
EMULATOR_VERSION = "1.22.0"
EMULATOR_SHA256 = "9b6498b7f62714d67f48f59b3818883cd682dbcd46b9f59511de81c97bb5166c"
EMULATOR_URL = (
    "https://storage.googleapis.com/firebase-preview-drop/emulator/"
    f"cloud-firestore-emulator-v{EMULATOR_VERSION}.jar"
)


def run(command: list[str], *, cache: Path, java: str) -> int:
    """
    Purpose: Start an isolated emulator and stop it after the command exits.
    Input: A command, cache path, and Java 21 executable.
    Output: The command's exit code, with emulator diagnostics retained in the cache.
    """
    cache.mkdir(parents=True, exist_ok=True)
    jar = cache / f"firestore-{EMULATOR_VERSION}.jar"
    if not jar.exists():
        with urlopen(EMULATOR_URL, timeout=180) as source, jar.open("wb") as output:
            while chunk := source.read(1024 * 1024):
                output.write(chunk)
    if hashlib.sha256(jar.read_bytes()).hexdigest() != EMULATOR_SHA256:
        raise ValueError("Firestore emulator checksum mismatch; remove the cached JAR")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    with (cache / "emulator.log").open("w") as log:
        process = subprocess.Popen([
            java, "-Xmx1g", "-jar", str(jar.resolve()), "--host=127.0.0.1",
            f"--port={port}", "--project_id=demo-backprop-tests",
        ], stdout=log, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 60
            while True:
                if process.poll() is not None:
                    raise RuntimeError("Firestore emulator exited; inspect emulator.log")
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                        break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Firestore emulator did not start within 60 seconds")
                    time.sleep(0.05)
            environment = {**os.environ, "FIRESTORE_EMULATOR_HOST": f"127.0.0.1:{port}"}
            return subprocess.run(command, env=environment).returncode
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path, default=Path(".backprop/firestore-emulator"),
                        help="Private emulator download and diagnostic directory")
    parser.add_argument("--java", default="java", help="Java 21 executable")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="Command after --")
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("Supply the test command after --")
    raise SystemExit(run(command, cache=args.cache, java=args.java))


if __name__ == "__main__":
    main()
