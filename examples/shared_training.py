"""Launch independent processes that contribute to one shared experiment.

Architecture: primary handle -> spawned local workers with separate journals ->
worker-local gradient descent -> parent join -> combined checkpoint and finish.
This demonstrates shared logging, not synchronized distributed optimization.
Main caller: CLI. No scheduler, GPU or external process service is required.
"""
import json
import multiprocessing
from pathlib import Path
import time

import backprop as wandb
from common import parser, positive_int, prepare, record, settings
from synthetic import make_data, measure, train_step


def worker(args, run_id, rank):
    """Train one independent shard and publish under a distinct writer label.

    Main caller: spawned Process. Common epoch values give charts a comparable
    axis; local step indices and unique config keys remain writer-specific.
    """
    label = f'worker-{rank}'
    with wandb.init(project=args.project, entity=args.entity, id=run_id,
                    name='Shared / independent shards', group=args.execution, mode='shared',
                    dir=str(args.output / label), tags=['demo', 'synthetic-data'],
                    config={label: {'seed': 100 + rank, 'learning_rate': 0.1 + rank * .04}},
                    settings=settings(x_label=label, x_primary=False)) as run:
        rows = make_data(100 + rank, 64)
        weights = [0.0] * 3
        run.define_metric('epoch')
        run.define_metric('train/*', step_metric='epoch')
        for epoch in range(args.steps):
            weights = train_step(weights, rows, .1 + rank * .04)
            loss, accuracy, _ = measure(weights, rows)
            run.log({'epoch': epoch, 'train/loss': loss, 'train/accuracy': accuracy},
                    step=epoch, commit=True)
        result = {'rank': rank, 'weights': weights, 'loss': loss, 'accuracy': accuracy}
        run.summary[label] = result
    (args.output / f'worker-{rank}.json').write_text(json.dumps(result))


def run_shared(args):
    """Keep the primary alive until every worker exits successfully.

    Main caller: main. A failed or timed-out worker fails the primary instead of
    presenting an incomplete experiment as finished. Successful joins coordinate
    processes; Foundry reads can still lag acknowledged uploads.
    """
    args.mode = 'shared'
    processes = []
    with wandb.init(project=args.project, entity=args.entity, id=args.execution,
                    name='Shared / independent shards', group=args.execution, mode='shared',
                    dir=str(args.output / 'primary'), tags=['demo', 'synthetic-data'],
                    config={'workers': args.workers, 'epochs': args.steps},
                    settings=settings(x_label='primary', x_primary=True)) as primary:
        try:
            context = multiprocessing.get_context('spawn')
            for rank in range(args.workers):
                process = context.Process(target=worker, args=(args, primary.id, rank))
                process.start()
                processes.append(process)
            deadline = time.monotonic() + args.timeout
            for process in processes:
                process.join(max(0, deadline - time.monotonic()))
                if process.is_alive():
                    raise TimeoutError('Worker deadline exceeded; inspect the retained local journals')
                if process.exitcode != 0:
                    raise RuntimeError(f'Worker exited with status {process.exitcode}')
            results = [json.loads((args.output / f'worker-{rank}.json').read_text())
                       for rank in range(args.workers)]
            primary.log({'workers/results': wandb.Table(columns=['rank', 'loss', 'accuracy'],
                         data=[[r['rank'], r['loss'], r['accuracy']] for r in results])})
            checkpoint = wandb.Artifact(args.execution + '-shards', 'model',
                                       description='Independent shard weights, not a synchronized model')
            for rank in range(args.workers):
                checkpoint.add_file(args.output / f'worker-{rank}.json')
            primary.log_artifact(checkpoint, aliases=['latest']).wait(timeout=120)
            primary.summary['completed_workers'] = len(results)
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate()
                process.join()
    return record(args, [primary], workers=results)


def main():
    cli = parser(__doc__, online_only=True)
    cli.add_argument('--steps', type=positive_int, default=30)
    cli.add_argument('--workers', type=positive_int, default=3)
    cli.add_argument('--timeout', type=positive_int, default=300, help='Total worker deadline in seconds')
    run_shared(prepare(cli.parse_args(), 'shared'))


if __name__ == '__main__':
    main()
