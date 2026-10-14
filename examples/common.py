"""Shared CLI and output handling for runnable SDK examples.

Architecture: CLI -> ordinary SDK configuration -> isolated local demo directory
-> small result manifest. Credentials are never copied into manifests.
Main callers: the example entry points. Why this exists: examples share setup,
but keep their actual logging and training operations visible in each script.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import uuid

import backprop as wandb


def positive_int(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError('must be a positive integer')
    return number


def parser(description, *, online_only=False):
    result = argparse.ArgumentParser(description=description)
    result.add_argument('--project', default='backprop-examples')
    result.add_argument('--entity', default='backprop')
    result.add_argument('--config', type=Path, help='SDK connection JSON, not an operator deployment config')
    result.add_argument('--mode', choices=['online'] if online_only else ['offline', 'online'],
                        default='online' if online_only else 'offline')
    result.add_argument('--output', type=Path, default=Path('.backprop/examples'))
    return result


def prepare(args, demo):
    """Resolve public SDK config and create a unique directory for this execution.

    Main callers: each demo's main. A new suffix prevents checkpoint/artifact
    names from colliding across invocations. Offline examples need no credential.
    """
    if args.config:
        path = args.config.expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f'SDK connection file does not exist: {path}')
        os.environ['BACKPROP_CONFIG'] = str(path)
    args.execution = f'{demo}-{uuid.uuid4().hex[:8]}'
    args.output = args.output.expanduser().resolve() / args.execution
    args.output.mkdir(parents=True)
    os.environ['BACKPROP_DIR'] = str(args.output)
    return args


def settings(**overrides):
    # Keep examples focused on their authored synthetic inputs. The torch demo
    # opts into local system measurements explicitly.
    return wandb.Settings(disable_git=True, save_code=False, console='off',
                          **{'x_disable_stats': True, **overrides})


def start(args, name, **kwargs):
    return wandb.init(project=args.project, entity=args.entity, mode=args.mode,
                      dir=str(args.output), name=name, group=args.execution,
                      tags=['demo', 'synthetic-data'], settings=settings(), **kwargs)


def record(args, runs, **details):
    """Print URLs or offline sync commands and retain only non-secret outputs.

    Main callers: completed demos. Why this exists: offline files are useful only
    if users can locate the exact journals that the SDK sync command accepts.
    """
    import shlex
    report = {'demo': args.execution, 'project': args.project, 'entity': args.entity,
              'mode': args.mode, 'runs': [
                  {'id': run.id, 'name': run.name, 'url': run.url, 'journal': run.sync_dir}
                  for run in runs], **details}
    target = args.output / 'result.json'
    target.write_text(json.dumps(report, indent=2) + '\n')
    print(f'Results: {target}', flush=True)
    for run in runs:
        if details.get('uploaded'):
            print(f'Uploaded run: {run.path}. Open this project in Backprop.', flush=True)
        elif args.mode == 'offline':
            print('Upload later: python -m backprop sync ' + shlex.quote(run.sync_dir), flush=True)
        elif run.url:
            print(f'Open: {run.url}', flush=True)
        else:
            print(f'Run: {run.path}', flush=True)
    return report
