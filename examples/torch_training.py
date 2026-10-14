"""Train a tiny nonlinear classifier with PyTorch measurements and checkpoints.

Architecture: seeded CPU tensors -> MLP/backward -> watch hooks -> metrics,
histograms, observed graph and model artifact. No GPU is selected implicitly.
Main caller: CLI. Optional dependencies: torch, numpy and Pillow.
"""
import backprop as wandb
from common import parser, positive_int, prepare, record, settings


def train(args):
    """Log real gradients and remove hooks even if training raises.

    Main caller: main. Checkpoints include optimizer state so the saved file is
    useful for continuation, not just a screenshot-producing model snapshot.
    """
    import torch
    torch.manual_seed(7)
    torch.set_num_threads(1)
    x = torch.randn(256, 2)
    y = (x[:, 0] * x[:, 1] > 0).long()
    model = torch.nn.Sequential(torch.nn.Linear(2, 16), torch.nn.Tanh(), torch.nn.Linear(16, 2))
    optimizer = torch.optim.Adam(model.parameters(), lr=.03)
    with wandb.init(project=args.project, entity=args.entity, mode=args.mode,
                    name='Torch / nonlinear classifier', group=args.execution,
                    dir=str(args.output), tags=['demo', 'synthetic-data'],
                    config={'epochs': args.steps, 'learning_rate': .03, 'device': 'cpu'},
                    settings=settings(x_disable_stats=False, x_stats_sampling_interval=1)) as run:
        run.define_metric('epoch')
        run.define_metric('train/*', step_metric='epoch')
        run.watch(model, log='all', log_freq=5, log_graph=True)
        try:
            for epoch in range(args.steps):
                optimizer.zero_grad()
                logits = model(x)
                loss = torch.nn.functional.cross_entropy(logits, y)
                loss.backward()
                optimizer.step()
                accuracy = (logits.argmax(dim=1) == y).float().mean().item()
                run.log({'epoch': epoch, 'train/loss': loss.item(), 'train/accuracy': accuracy})
            points = wandb.Table(columns=['x', 'y', 'label', 'prediction'], data=[
                [float(point[0]), float(point[1]), int(label), int(prediction)]
                for point, label, prediction in zip(x[:32], y[:32], logits.argmax(dim=1)[:32])])
            run.log({'evaluation/examples': points})
            checkpoint = args.output / 'checkpoint.pt'
            torch.save({'model': model.state_dict(), 'optimizer': optimizer.state_dict(),
                        'epoch': args.steps - 1}, checkpoint)
            artifact = wandb.Artifact(args.execution + '-model', 'model', metadata={'framework': 'pytorch'})
            artifact.add_file(checkpoint)
            run.log_artifact(artifact, aliases=['latest'])
            run.summary['final_accuracy'] = accuracy
        finally:
            run.unwatch(model)
    return record(args, [run], final_loss=loss.item(), final_accuracy=accuracy)


def main():
    cli = parser(__doc__)
    cli.add_argument('--steps', type=positive_int, default=80)
    train(prepare(cli.parse_args(), 'torch'))


if __name__ == '__main__':
    main()
