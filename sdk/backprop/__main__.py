"""
Purpose: Provide launch, synchronization, import, and local dashboard commands.
Input: CLI arguments and an optional connection configuration.
Output: A command result or a local dashboard server.
Note: The launch command changes import aliases only in its own process.
"""
import argparse
import os
from pathlib import Path
import runpy
import sys

import backprop
from .transport import load_config


def main(argv: list[str] | None = None) -> None:
    """
    Purpose: Dispatch one explicit CLI action without a global import alias.
    Input: Optional command arguments, or the process argument list.
    Output: A completed command or a local server until the user stops it.
    """
    parser = argparse.ArgumentParser(prog="backprop")
    commands = parser.add_subparsers(dest="command", required=True)
    sync_parser = commands.add_parser("sync", help="Upload one offline run directory")
    sync_parser.add_argument("directory")
    sync_parser.add_argument("--config")
    from backprop_backend.cli import add_commands, dispatch
    add_commands(commands)
    run_parser = commands.add_parser("run", help="Run Python with import wandb redirected to Backprop")
    run_parser.add_argument("--high-command", action="store_true", help="Enable High Command's W&B credential readiness gate")
    run_parser.add_argument("target", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if dispatch(args):
        return
    if args.command == "sync":
        count = backprop.sync(args.directory, config=load_config(args.config))
        print(f"Backprop synced {count} events")
        return
    target = args.target
    if target and target[0] == "--":
        target = target[1:]
    if not target:
        parser.error("run requires a script path or -- -m module_name")
    if args.high_command:
        # High Command checks this variable before importing its logging module.
        # This sentinel is not an authentication token and is never transmitted.
        os.environ["WANDB_API_KEY"] = "backprop-foundry-readiness-sentinel"
        print("Backprop: High Command readiness shim enabled; configured backend credentials are used for logging.", file=sys.stderr)
    from .compatibility import install_wandb_aliases
    install_wandb_aliases()
    try:
        if target[0] == "-m":
            if len(target) < 2:
                parser.error("-m requires a module name")
            sys.argv = target[1:]
            runpy.run_module(target[1], run_name="__main__", alter_sys=True)
        else:
            script = Path(target[0]).resolve()
            sys.argv = [str(script), *target[1:]]
            sys.path.insert(0, str(script.parent))
            runpy.run_path(str(script), run_name="__main__")
    except BaseException as error:
        code = error.code if isinstance(error, SystemExit) else 1
        code = code if isinstance(code, int) else (0 if code is None else 1)
        for active in backprop._runs:
            if not active._closed:
                active._unhandled_exit_code = code
        raise


if __name__ == "__main__":
    main()
