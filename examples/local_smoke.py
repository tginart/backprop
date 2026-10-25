"""
Purpose: Start the installed local dashboard and a small real SDK experiment.
Input: An optional port, step count, and delay between metric records.
Output: Live metrics, a table, a Plotly chart, and a verified model artifact.
Note: The server and all data belong to this isolated demo directory.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
from urllib.parse import urlencode
import uuid
import webbrowser


def main() -> None:
    """
    Purpose: Own the local CLI process until the user exits the demo.
    Input: Command arguments and the installed Backprop SDK.
    Output: A browser URL and durable demo data after shutdown.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, default=Path('.backprop/local-smoke'),
                        help='Persistent demo directory (default: .backprop/local-smoke)')
    parser.add_argument('--port', type=int, default=8765,
                        help='Loopback port; use 0 to choose a free port')
    parser.add_argument('--steps', type=int, default=120,
                        help='Number of optimization steps (default: 120)')
    parser.add_argument('--interval', type=float, default=0.5,
                        help='Seconds between metric records (default: 0.5)')
    parser.add_argument('--no-open', action='store_true',
                        help='Print the dashboard URL without a browser launch')
    parser.add_argument('--exit-after-demo', action='store_true',
                        help='Stop the server after the SDK assertions pass')
    args = parser.parse_args()
    if args.steps < 1 or args.interval < 0:
        parser.error('--steps must be positive and --interval must be nonnegative')

    args.directory = args.directory.expanduser().resolve()
    data = args.directory / 'data'
    server = subprocess.Popen(
        [sys.executable, '-u', '-m', 'backprop', 'serve', str(data),
         '--port', str(args.port)],
        stdout=subprocess.PIPE, text=True, start_new_session=True,
    )
    try:
        origin = _read_startup(server)
        os.environ['BACKPROP_CONFIG'] = str(data / 'connection.json')
        for name in ('BACKPROP_BACKEND', 'BACKPROP_HOST', 'BACKPROP_APP_URL',
                     'BACKPROP_STREAM_DATASET_RID', 'BACKPROP_MEDIA_DATASET_RID',
                     'WANDB_BASE_URL'):
            os.environ.pop(name, None)
        _train(origin, args)
        print('Data: ' + str(data), flush=True)
        if not args.exit_after_demo:
            print('The dashboard stays up. Press Ctrl-C to stop it.', flush=True)
            server.wait()
    except KeyboardInterrupt:
        print('\nStopping this demo. Data stays on disk.', flush=True)
    finally:
        if server.poll() is None:
            if os.name == 'nt':
                server.terminate()
            else:
                server.send_signal(signal.SIGINT)
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait()
        server.stdout.close()


def _read_startup(server: subprocess.Popen) -> str:
    """
    Purpose: Wait for the CLI readiness message with a bounded startup wait.
    Input: The demo's new server process.
    Output: The loopback origin, or an explicit startup error.
    """
    ready = threading.Event()
    result = []

    def read_line() -> None:
        try:
            result.append(server.stdout.readline().strip())
        except Exception as error:
            result.append(error)
        finally:
            ready.set()

    threading.Thread(target=read_line, daemon=True).start()
    if not ready.wait(timeout=15):
        raise TimeoutError('The local server did not start within 15 seconds')
    line = result[0]
    if isinstance(line, Exception):
        raise line
    prefix = 'Backprop dashboard: '
    if not line.startswith(prefix):
        raise RuntimeError('Could not start the local server; check the port above. ' + line)
    return line.removeprefix(prefix).rstrip('/')


def _train(origin: str, args: argparse.Namespace) -> None:
    """
    Purpose: Minimize a scalar squared error with ordinary public SDK calls.
    Input: The local server and the requested experiment duration.
    Output: A live run, rich outputs, and SDK readback assertions.
    """
    import backprop as wandb

    stopped = threading.Event()
    identifier = 'smoke-' + uuid.uuid4().hex[:8]
    url = origin + '/?' + urlencode({'project': 'local/local-smoke', 'run': identifier})
    target, weight, learning_rate = 3.0, -2.0, 0.025
    grid = list(range(-3, 7))
    with wandb.init(
        project='local-smoke', entity='local', id=identifier,
        name='Local smoke ' + identifier.removeprefix('smoke-'), mode='online',
        dir=args.directory / 'runs',
        config={'task': 'Minimize (weight - 3)^2', 'learning_rate': learning_rate},
        settings={'console': 'off', 'silent': True, 'disable_git': True,
                  'x_disable_stats': True, 'flush_interval': 0.5,
                  'stop_fn': stopped.set},
    ) as run:
        for step in range(args.steps):
            if stopped.is_set():
                break
            weight -= learning_rate * 2 * (weight - target)
            metrics = {'train/loss': (weight - target) ** 2, 'train/weight': weight}
            if step == 0:
                metrics['outputs/table'] = wandb.Table(
                    columns=['input', 'target'], data=[[x, target] for x in grid])
                metrics['outputs/plotly'] = wandb.Plotly({
                    'data': [{'type': 'scatter', 'mode': 'lines+markers',
                              'x': grid, 'y': [(x - target) ** 2 for x in grid]}],
                    'layout': {'title': {'text': 'Objective: (weight - 3)²'}},
                })
            run.log(metrics, step=step, commit=True)
            if step == 0:
                run.flush()
                print('\nDashboard: ' + url, flush=True)
                print('Watch train/loss. Outputs contains the table and Plotly chart.', flush=True)
                print('The run menu also supports a cooperative stop request.\n', flush=True)
                if not args.no_open:
                    webbrowser.open(url)
            if step % 20 == 0:
                print(f'Step {step:3d}: loss={metrics["train/loss"]:.6f}', flush=True)
            stopped.wait(args.interval)

        model = {'weight': weight, 'target': target, 'loss': (weight - target) ** 2}
        artifact_name = identifier + '-model'
        artifact = wandb.Artifact(artifact_name, type='model')
        with artifact.new_file('model.json') as output:
            json.dump(model, output, indent=2)
        run.log_artifact(artifact)
        run.summary['final_loss'] = model['loss']

    api = wandb.Api()
    history = api.run('local/local-smoke/' + identifier).history(pandas=False)
    assert history and 'train/loss' in history[-1], 'Metric readback failed'
    assert any('outputs/table' in row and 'outputs/plotly' in row for row in history)
    restored = api.artifact('local/local-smoke/' + artifact_name + ':latest')
    download = restored.get_entry('model.json').download(root=str(args.directory / 'downloads' / identifier))
    assert json.loads(Path(download).read_text()) == model
    print('\nSMOKE PASSED: metrics, table, Plotly, artifact upload and verified download.', flush=True)


if __name__ == '__main__':
    main()
