"""Query experiments and export their scalar summaries and complete histories.

Architecture: authenticated public Api -> project runs -> local summary/history
exports. Read-only: this script does not edit or stop experiments.
Main caller: CLI, after the logging demos have become visible in Foundry.
"""
from collections.abc import Mapping
import csv
import json

import backprop as wandb
from common import parser, prepare


def plain(value):
    """Copy nested public summary mappings into ordinary JSON containers.

    Main caller: inspect. Public summary views remain editable SDK objects even
    inside dict(run.summary); recursively copy them without stringifying values.
    """
    if isinstance(value, Mapping):
        return {key: plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(item) for item in value]
    return value


def inspect(args):
    """Export a fresh project snapshot without modifying remote records.

    Main caller: main. These deliberately small examples retain rich history
    values in JSON while the CSV provides a convenient cross-run summary.
    """
    api = wandb.Api()
    rows = []
    for run in api.runs(f'{args.entity}/{args.project}', order='+created_at'):
        if args.group and run.group != args.group:
            continue
        history = list(run.scan_history())
        # JSON retains nested cells and writer metadata instead of forcing those
        # values into CSV. CSV is reserved for the cross-run scalar overview.
        target = args.output / f'{run.id}-history.json'
        target.write_text(json.dumps(history, indent=2) + '\n')
        rows.append({'id': run.id, 'name': run.name, 'state': run.state,
                     'group': run.group, 'history_rows': len(history),
                     'summary': json.dumps(plain(run.summary)), 'history_file': target.name})
        print(f'{run.name}: {run.state}, {len(history)} history rows')
    with (args.output / 'runs.csv').open('w', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=['id', 'name', 'state', 'group', 'history_rows', 'summary', 'history_file'])
        writer.writeheader()
        writer.writerows(rows)
    print(f'Exported {len(rows)} runs to {args.output}')
    return rows


def main():
    cli = parser(__doc__, online_only=True)
    cli.add_argument('--group', help='Optional exact execution group printed in a demo result.json')
    inspect(prepare(cli.parse_args(), 'inspection'))


if __name__ == '__main__':
    main()
