"""Log inspectable 2D/3D Plotly figures from a measured optimization trajectory.

Architecture: seeded samples -> logistic training -> parameter/loss trajectory ->
Plotly figures plus the underlying Table. The viewer uses its bundled renderer.
Main caller: CLI. Requires the plots extra; no hosted chart provider is used.
"""
import backprop as wandb
from common import parser, positive_int, prepare, record, start
from synthetic import make_data, measure, train_step


def gallery(args):
    """Publish both rendered figures and their numerical source for inspection.

    Main caller: main. The explicit dark template remains part of the saved
    figure, so rendering does not depend on implicit browser theme defaults.
    """
    import plotly.graph_objects as go
    rows = make_data(19, 128)
    weights = [0.0] * 3
    trajectory = []
    with start(args, 'Plotly / optimization trajectory', config={'epochs': args.steps}) as run:
        run.define_metric('epoch')
        run.define_metric('train/*', step_metric='epoch')
        for epoch in range(args.steps):
            weights = train_step(weights, rows, .2)
            loss, accuracy, _ = measure(weights, rows)
            trajectory.append([epoch, *weights, loss, accuracy])
            run.log({'epoch': epoch, 'train/loss': loss, 'train/accuracy': accuracy})
        curve = go.Figure(go.Scatter(x=[r[0] for r in trajectory], y=[r[4] for r in trajectory],
                                    mode='lines+markers', name='Training loss'))
        curve.update_layout(template='plotly_dark', paper_bgcolor='#232830', plot_bgcolor='#232830', title='Optimization progress', xaxis_title='Epoch', yaxis_title='Loss')
        path = go.Figure(go.Scatter3d(x=[r[1] for r in trajectory], y=[r[2] for r in trajectory],
                                    z=[r[4] for r in trajectory], mode='lines+markers',
                                    marker={'color': [r[0] for r in trajectory], 'colorscale': 'Viridis', 'size': 4},
                                    text=[f'epoch {r[0]}' for r in trajectory], name='Parameter trajectory'))
        path.update_layout(template='plotly_dark', paper_bgcolor='#232830', title='Weights and measured loss',
                           scene={'xaxis_title': 'Weight x', 'yaxis_title': 'Weight y', 'zaxis_title': 'Loss'})
        run.log({'plots/progress': wandb.Plotly(curve), 'plots/parameter_path': wandb.Plotly(path),
                 'tables/trajectory': wandb.Table(columns=['epoch', 'weight_x', 'weight_y', 'bias', 'loss', 'accuracy'],
                                                 data=trajectory)})
    return record(args, [run], points=len(trajectory))


def main():
    cli = parser(__doc__)
    cli.add_argument('--steps', type=positive_int, default=40)
    gallery(prepare(cli.parse_args(), 'plotly'))


if __name__ == '__main__':
    main()
