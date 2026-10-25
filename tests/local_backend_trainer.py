"""
Purpose: Keep one real SDK writer active for the local browser smoke test.
Input: A connection file and a temporary run directory.
Output: Live metrics, a table, a Plotly figure, an artifact, and stop status.
"""
import json
import os
from pathlib import Path
import select
import sys
import threading

import backprop
import backprop.run_stopping


def main() -> None:
    """
    Purpose: Log real SDK values and accept one additional metric on stdin.
    Input: The run directory and line-delimited test commands.
    Output: A live run and JSON markers for browser coordination.
    """
    directory = Path(sys.argv[1])
    directory.mkdir(parents=True, exist_ok=True)
    initial_offsets = None
    if os.environ.get("BACKPROP_TEST_CAPTURE_OFFSETS") == "1":
        from backprop.transport import load_config, native_store
        store = native_store(load_config())
        initial_offsets = store.request(store.stream_path + "/getEndOffsets?" + store._stream_query())
    stopped = threading.Event()
    backprop.run_stopping.POLL_INTERVAL = 0.05
    run = backprop.init(
        project=os.environ.get("BACKPROP_TEST_PROJECT", "local-smoke"),
        entity=os.environ.get("BACKPROP_TEST_ENTITY", "local"), name="Local browser smoke",
        id=os.environ.get("BACKPROP_TEST_RUN_ID", "browser-smoke"), dir=directory,
        settings={"console": "off", "disable_git": True, "x_disable_stats": True,
                  "flush_interval": 0.1, "stop_fn": stopped.set},
    )
    try:
        for step in range(5):
            run.log({"train/loss": 1 / (step + 1)}, step=step, commit=True)
        run.log({
            "outputs/table": backprop.Table(columns=["name", "score"],
                                            data=[["local table row", 0.9]]),
            "outputs/plotly": backprop.Plotly({
                "data": [{"type": "scatter", "x": [0, 1], "y": [1, 2]}],
                "layout": {"title": {"text": "Local Plotly"}},
            }),
        })
        source = directory / "weights.txt"
        source.write_text("local artifact bytes")
        artifact = backprop.Artifact("local-model", type="model")
        artifact.add_file(str(source))
        run.log_artifact(artifact)
        run.flush()
        print("BACKPROP_FIXTURE " + json.dumps({"ready": True, "initial_offsets": initial_offsets}), flush=True)
        while not stopped.is_set():
            ready, _, _ = select.select([sys.stdin], [], [], 0.05)
            if not ready:
                continue
            command = sys.stdin.readline().strip()
            if command == "log":
                run.log({"train/loss": 0.0125}, step=10, commit=True)
                run.flush()
                print("BACKPROP_FIXTURE " + json.dumps({"logged": True}), flush=True)
            elif command == "finish" or not command:
                break
    finally:
        run.finish()
    print("BACKPROP_FIXTURE " + json.dumps({"stopped": stopped.is_set()}), flush=True)


if __name__ == "__main__":
    main()
