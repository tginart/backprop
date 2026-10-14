"""Keep subprocess diagnostics separate from framework observation records.

Architecture: caller-supplied JSON filename -> real isolated process -> exact
stdout/stderr byte files -> caller checks status and parses only that filename.
Main callers: optional framework comparisons and the real chart regression.
Why this exists: CatBoost writes native training messages to the process file
descriptor, which Python's redirect_stdout cannot reserve for a JSON protocol.
"""
from pathlib import Path
import subprocess


def run_observation(command, output, *, cwd, env, timeout):
    """Run one observation process, retaining diagnostics even when it fails.

    Main callers: compare_family and PinnedFrameworkSources chart regression.
    The child receives its required output path as the final argument. Remove
    any stale result first; stdout/stderr are never searched or trimmed for JSON.
    A timeout preserves every captured byte before the original error propagates.
    """
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.unlink(missing_ok=True)
    stdout_path = output.with_suffix('.stdout.log')
    stderr_path = output.with_suffix('.stderr.log')
    try:
        result = subprocess.run([*command, str(output)], cwd=cwd, env=env,
                                capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as error:
        stdout_path.write_bytes(error.stdout or b'')
        stderr_path.write_bytes(error.stderr or b'')
        raise
    stdout_path.write_bytes(result.stdout)
    stderr_path.write_bytes(result.stderr)
    return result
