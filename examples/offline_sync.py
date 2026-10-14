"""Record offline first, then optionally upload and replay the same journal twice.

Architecture: local training -> durable events/files -> explicit sync -> repeated
sync -> unchanged acknowledgement cursor. No connection is needed without --sync.
Main caller: CLI. This demonstrates retry-safe replay, not automatic model resume.
"""
import backprop as wandb
from common import parser, positive_int, prepare, record, start
from synthetic import make_data, measure, train_step


def demonstrate(args):
    """Finish a local journal before optionally publishing and retrying it.

    Main caller: main. An explicit second sync makes the lack of new pending
    events observable instead of assuming that retries cannot duplicate work.
    """
    args.mode = 'offline'
    rows = make_data(29, 64)
    weights = [0.0] * 3
    with start(args, 'Offline / replayable training', config={'epochs': args.steps}) as run:
        run.define_metric('epoch')
        run.define_metric('train/*', step_metric='epoch')
        checkpoints = wandb.Table(columns=['epoch', 'loss'], log_mode='INCREMENTAL')
        for epoch in range(args.steps):
            weights = train_step(weights, rows, .2)
            loss, accuracy, _ = measure(weights, rows)
            checkpoints.add_data(epoch, loss)
            run.log({'epoch': epoch, 'train/loss': loss, 'train/accuracy': accuracy,
                     'training/checkpoints': checkpoints})
        run.summary['final_loss'] = loss
    details = {}
    if args.sync:
        # Normal SDK environment/config selects the destination and credentials.
        first = wandb.sync(run.sync_dir)
        second = wandb.sync(run.sync_dir)
        # sync returns the cumulative acknowledged cursor, not this call's count.
        if second != first:
            raise RuntimeError(f'Unexpected acknowledgement change on retry: {first} -> {second}')
        details = {'uploaded': True, 'acknowledged_events': first, 'retry_acknowledged_events': second}
        print(f'Acknowledged {first} events; retry cursor is unchanged at {second}.')
    return record(args, [run], **details)


def main():
    cli = parser(__doc__)
    cli.add_argument('--steps', type=positive_int, default=12)
    cli.add_argument('--sync', action='store_true', help='Upload to configured Foundry, then verify an empty retry')
    args = cli.parse_args()
    if args.mode != 'offline':
        cli.error('This demo records offline. Use --sync to upload afterwards.')
    demonstrate(prepare(args, 'offline'))


if __name__ == '__main__':
    main()
