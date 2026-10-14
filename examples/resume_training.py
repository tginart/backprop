"""Continue an experiment from an explicit checkpoint and the same run ID.

Architecture: first invocation trains and checkpoints -> later invocation loads
weights/next epoch -> SDK resume='must' restores the online experiment -> train.
Main caller: CLI, twice. SDK resume restores logging state, not model weights.
"""
import json
from pathlib import Path

import backprop as wandb
from common import parser, positive_int, prepare, record, settings
from synthetic import make_data, measure, train_step


def train(args):
    """Save enough local state to reproduce an uninterrupted optimization.

    Main caller: main. Scope is validated before opening an existing run; exact
    local step values prevent an apparent successful replay of old measurements.
    """
    if args.checkpoint:
        state = json.loads(args.checkpoint.read_text())
        if (state['entity'], state['project']) != (args.entity, args.project):
            raise ValueError('Checkpoint entity/project must match the command')
        run_id, weights, first_epoch = state['run_id'], state['weights'], state['next_epoch']
        group = state['group']
    else:
        run_id, weights, first_epoch = args.execution, [0.0] * 3, 0
        group = args.execution
    rows = make_data(13, 128)
    with wandb.init(project=args.project, entity=args.entity, id=run_id,
                    mode='online', resume='must' if args.checkpoint else 'never',
                    name='Resume / checkpointed classifier', group=group,
                    tags=['demo', 'synthetic-data'], dir=str(args.output),
                    config=None if args.checkpoint else {'seed': 13, 'learning_rate': .2},
                    settings=settings()) as run:
        if args.checkpoint and run.step != first_epoch:
            raise RuntimeError(f'Checkpoint expects step {first_epoch}, but visible run is at {run.step}; '
                               'wait for visibility or inspect the checkpoint before retrying')
        run.define_metric('epoch')
        run.define_metric('train/*', step_metric='epoch')
        for epoch in range(first_epoch, first_epoch + args.steps):
            weights = train_step(weights, rows, .2)
            loss, accuracy, _ = measure(weights, rows)
            run.log({'epoch': epoch, 'train/loss': loss, 'train/accuracy': accuracy},
                    step=epoch, commit=True)
        run.summary['completed_epochs'] = first_epoch + args.steps
    checkpoint = args.output / 'checkpoint.json'
    checkpoint.write_text(json.dumps({'entity': args.entity, 'project': args.project,
        'run_id': run_id, 'group': group, 'weights': weights, 'next_epoch': first_epoch + args.steps}, indent=2))
    return record(args, [run], checkpoint=str(checkpoint), next_epoch=first_epoch + args.steps)


def main():
    cli = parser(__doc__, online_only=True)
    cli.add_argument('--steps', type=positive_int, default=20, help='Additional epochs in this invocation')
    cli.add_argument('--checkpoint', type=Path, help='checkpoint.json printed by the previous invocation')
    train(prepare(cli.parse_args(), 'resume'))


if __name__ == '__main__':
    main()
