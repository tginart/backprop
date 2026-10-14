"""Replay High Command's real observer fixtures through the actual SDK.

Architecture: an optional sibling synthetic-project checkout provides its real
WandbMirror and compact recorded job events. Only package metadata is mocked;
all logging, histogram, table, artifact, resume, and finish code is real.
Main callers: HIGH_COMMAND_REFERENCE=/path/to/repo python -m unittest discover.
"""
import contextlib
import gzip
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sdk"))
import backprop

REFERENCE = Path(os.environ.get("HIGH_COMMAND_REFERENCE", "__no_reference__"))


@unittest.skipUnless((REFERENCE / "src/high_command/wandb_logging.py").exists(),
                     "Set HIGH_COMMAND_REFERENCE to a High Command checkout")
@unittest.skipUnless(importlib.util.find_spec("numpy"), "High Command's histogram replay requires NumPy")
class HighCommandReplayTests(unittest.TestCase):
    def test_real_observer_initial_run_and_checkpoint_resume(self):
        sys.path.insert(0, str(REFERENCE / "src"))
        from high_command.wandb_logging import WandbMirror
        from high_command import observer_package
        fixture_dir = REFERENCE / "tests/fixtures/wandb_events"
        batches = []
        for name in ["dp2-graduated.job-events.jsonl.gz", "dp2-graduated-resume.job-events.jsonl.gz"]:
            with gzip.open(fixture_dir / name, "rt") as source:
                batches.append([json.loads(line) for line in source])
        ids = {event["task_id"] for events in batches for event in events if "task_id" in event}
        references = {task_id: {"trajectory_id": task_id.split("#")[0], "corpus": "fixture",
                               "category": "fixture", "pointer": int(task_id.split("#")[1]),
                               "original_step_index": int(task_id.split("#")[1]), "trainable_steps": 1,
                               "reference_action": {"tool": "fixture"}} for task_id in ids}
        with tempfile.TemporaryDirectory() as temporary, contextlib.ExitStack() as active_runs:
            root = Path(temporary)
            sink = backprop.MemorySink()
            module = SimpleNamespace(Table=backprop.Table, Histogram=backprop.Histogram,
                                     Artifact=backprop.Artifact,
                                     init=lambda **kwargs: backprop.Run(**kwargs, dir=root / "backprop",
                                            _sink=sink, _deployment={"app_url": "https://backprop.example"}))
            previous = None
            mirrors = []
            with patch.object(observer_package, "references", return_value=(references, "fixture-sha256")):
                with contextlib.redirect_stdout(io.StringIO()):
                    for index, events in enumerate(batches):
                        output = root / f"job-{index}"
                        output.mkdir()
                        mirror = WandbMirror(module, output=output, package=root / "package.json",
                                             name=f"fixture-{index}", project="synthetic-project-tests",
                                             config={"mode": "train", "group_size": 4},
                                             metadata=root / "state" / f"job-{index}.json", resume_metadata=previous)
                        # Close capture before its temporary directory disappears,
                        # including when a replay assertion or optional input fails.
                        active_runs.callback(lambda run=mirror.run: None if run._closed else run.finish(exit_code=1))
                        data = "".join(json.dumps(event) + "\n" for event in events).encode()
                        with mirror.events.open("ab") as stream:
                            for start in range(0, len(data), 65536):
                                stream.write(data[start:start + 65536])
                                stream.flush()
                                mirror.poll()
                        mirror.finish(0)
                        previous = mirror.metadata
                        mirrors.append(mirror)
            self.assertEqual(mirrors[0].run.id, mirrors[1].run.id)
            self.assertTrue(mirrors[1].run.resumed)
            # Closed runs expose no live Summary view. Verify each completed
            # attempt's persisted snapshot, including the resumed aggregation.
            summaries = [event["payload"]["values"] for event in sink.events
                         if event["type"] == "summary" and event["payload"].get("automatic")]
            self.assertEqual(len(summaries), 2)
            self.assertEqual(summaries[0]["overview/committed_groups"], 26)
            self.assertEqual(summaries[0]["overview/scored_rollouts"], 138)
            self.assertEqual(summaries[0]["overview/updates"], 9)
            self.assertEqual(summaries[1]["overview/committed_groups"], 28)
            rows = [event["payload"] for event in sink.events if event["type"] == "history"]
            self.assertEqual([row["step"] for row in rows], list(range(len(rows))))
            self.assertTrue(any(value.get("_type") == "histogram" for row in rows
                                for value in row["metrics"].values() if isinstance(value, dict)))
            self.assertTrue(any(value.get("log_mode") == "INCREMENTAL" and value["row_offset"] > 0 for row in rows
                                for value in row["metrics"].values() if isinstance(value, dict)))
            self.assertGreater(len(sink.files), 2)
            artifacts = [event for event in sink.events if event["type"] == "artifact"]
            self.assertTrue(any(event["payload"]["artifact_type"] == "synthetic-project-policy-context" for event in artifacts))
            self.assertEqual(sum(event["type"] == "run_finish" for event in sink.events), 2)


if __name__ == "__main__":
    unittest.main()
