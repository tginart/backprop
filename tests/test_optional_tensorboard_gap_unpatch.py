"""Unpatch removes new discovery hooks, not streams already owned by live runs.

Architecture: portable actual Run dispatch checks at the unavailable decoder
boundary -> isolated genuine writer/core comparisons when installed -> immutable
event bytes, source/native rows and native restart/retry evidence.
Main callers: optional TensorBoard CI discovery. Why this exists: a live tail is
not committed until finish, so deleting discovered paths at unpatch loses data.
"""
import base64
from contextlib import ExitStack
import hashlib
import importlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import backprop
from backprop.integration import tensorboard as tb
from backprop.transport import MemorySink, read_journal
from backprop.apis.public.replay import reduce_events
from framework_gap_process import run_observation
from reference_runtime import reference_runtime

ROOT = Path(__file__).resolve().parents[1]
ALIASES = ("wandb.tensorboard", "wandb.integration.tensorboard")
SETTINGS = {"silent": True, "console": "off", "x_disable_stats": True,
            "disable_git": True, "disable_code": True, "flush_interval": 3600}


def public_alias(alias):
    """Use the public root attribute or the independently imported module.

    Main callers: native portable lifecycle tests. Why this exists: pinned
    wandb.tensorboard must be accessed through its lazy public attribute before
    its module alias exists. Exact W&B shim routes have separate signature checks.
    """
    return backprop.tensorboard if alias == ALIASES[0] else importlib.import_module("backprop.integration.tensorboard")


class TensorBoardUnpatchAliasContracts(unittest.TestCase):
    def test_actual_public_attribute_and_canonical_module_before_writer_import(self):
        """Run the same probe's real alias selection without optional writers.

        Main caller: portable CI. Both SDKs resolve real public objects, call
        unpatch and inspect signatures; no absent module is injected. The real
        writer mode follows these exact selections after this preflight stage.
        """
        reference = reference_runtime()
        observations = {}
        for mode in ("reference", "native"):
            output = ROOT / "test-results" / ("tensorboard-gap-unpatch-alias-preflight-" + mode + ".json")
            env = {key: value for key, value in os.environ.items()
                   if not key.startswith(("WANDB_", "BACKPROP_", "FOUNDRY_"))}
            paths = [ROOT / "tests"] if mode == "reference" else [ROOT / "sdk", ROOT / "tests"]
            env.update(PYTHONPATH=os.pathsep.join(map(str, paths)), PYTHONDONTWRITEBYTECODE="1")
            result = run_observation([reference.python if mode == "reference" else sys.executable, "-B",
                str(ROOT / "tests/tensorboard_gap_unpatch_probe.py"), mode, "alias-preflight", ALIASES[0]],
                output, cwd=tempfile.gettempdir(), env=env, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
            observations[mode] = json.loads(output.read_text())
            self.assertTrue(all(observations[mode]["aliases"].values()))
            self.assertEqual(observations[mode]["empty_unpatch"], dict.fromkeys(ALIASES))
        self.assertEqual(observations["reference"]["root_module_import"],
                         {"error": "ModuleNotFoundError", "name": "wandb.tensorboard"})
        self.assertEqual(observations["reference"]["access"][ALIASES[0]], "wandb.tensorboard (attribute)")
        self.assertEqual(observations["reference"]["access"][ALIASES[1]], "wandb.integration.tensorboard (module)")
        self.assertEqual(observations["native"]["signatures"], observations["reference"]["signatures"])

    def test_actual_settings_and_nonwriter_lifecycle_before_optional_ingestion(self):
        """Validate the actual common lifecycle, separately from absent writers.

        Main caller: portable source/native probe regression. No SDK settings,
        init, logging, finish, core completion or native durable resume is
        replaced. Actual writer hooks and source decoding still require CI.
        """
        reference = reference_runtime()
        for mode in ("reference", "native"):
            with self.subTest(mode=mode):
                output = ROOT / "test-results" / ("tensorboard-gap-unpatch-lifecycle-preflight-" + mode + ".json")
                env = {key: value for key, value in os.environ.items()
                       if not key.startswith(("WANDB_", "BACKPROP_", "FOUNDRY_"))}
                paths = [ROOT / "tests"] if mode == "reference" else [ROOT / "sdk", ROOT / "tests"]
                env.update(PYTHONPATH=os.pathsep.join(map(str, paths)), PYTHONDONTWRITEBYTECODE="1",
                           WANDB_DATA_DIR="/tmp/backprop-reference-data")
                result = run_observation([reference.python if mode == "reference" else sys.executable, "-B",
                    str(ROOT / "tests/tensorboard_gap_unpatch_probe.py"), mode, "lifecycle-preflight", ALIASES[0]],
                    output, cwd=tempfile.gettempdir(), env=env, timeout=45)
                self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
                data = json.loads(output.read_text())
                self.assertTrue(data["global_owner"])
                self.assertTrue(data["primary_survives_secondary_finish"])
                self.assertEqual([row["lifecycle"] for row in data["first_rows"]], [1])
                self.assertEqual([row["nonglobal"] for row in data["second_rows"]], [2])
                if mode == "reference":
                    self.assertNotIn("flush_interval", data["settings"])
                else:
                    self.assertEqual(data["settings"]["flush_interval"], 3600)
                    self.assertEqual([row["lifecycle"] for row in data["restart"]["rows"]], [1])
                    self.assertEqual(data["restart"]["fresh_directories"], [])
                    self.assertTrue(data["restart"]["finished_sources_released"])


class TensorBoardUnpatchPortable(unittest.TestCase):
    def setUp(self):
        backprop.teardown()
        tb.unpatch()
        self.tmp = tempfile.TemporaryDirectory(prefix="tb-unpatch-portable-")
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(tb.unpatch)
        self.addCleanup(backprop.teardown)
        self.contexts = ExitStack()
        self.addCleanup(self.contexts.close)
        self.contexts.enter_context(patch("backprop.settings.in_jupyter", return_value=False))
        self.sink = MemorySink()
        self.runs = []
        self.writer_initializations = []
        writer_initializations = self.writer_initializations
        class Writer:
            def __init__(self, logdir):
                writer_initializations.append(logdir)
                Path(logdir).mkdir(parents=True, exist_ok=True)
        self.writer, self.original = Writer, Writer.__init__
        module = SimpleNamespace(EventFileWriter=Writer)
        self.contexts.enter_context(patch.object(tb, "_optional_module", side_effect=lambda name:
            module if name == "tensorboard.summary.writer.event_file_writer" else None))
        find_spec = importlib.util.find_spec
        self.contexts.enter_context(patch.object(importlib.util, "find_spec", side_effect=lambda name, *args, **kwargs:
            SimpleNamespace() if name == "tensorboard" else find_spec(name, *args, **kwargs)))
        self.contexts.enter_context(patch.object(tb, "sync", return_value=0))
        # addCleanup runs in reverse order: finish real runs while decoder and
        # installation boundaries remain active, then unwind contexts, restore
        # SDK hooks and finally remove files. ExitStack supports Python 3.10.
        self.addCleanup(self.finish_runs)

    def finish_runs(self):
        for run in reversed(self.runs):
            run.finish()

    def run_for(self, identifier, *, nonglobal=False, resume=None):
        if resume is not None:
            # Offline init intentionally ignores resume in both SDKs. A real
            # native handle with MemorySink restores its existing local journal
            # without introducing hosted authentication into this boundary.
            run = backprop.Run(id=identifier, project="tb-unpatch", entity="e", dir=self.tmp.name,
                mode="online", settings=SETTINGS, _sink=self.sink, resume=resume)
        else:
            run = backprop.init(id=identifier, project="tb-unpatch", entity="e", dir=self.tmp.name,
                mode="offline", settings=SETTINGS, _sink=self.sink,
                reinit="create_new" if nonglobal else "finish_previous")
        self.runs.append(run)
        return run

    def test_each_alias_preserves_announced_policy_and_final_dispatch(self):
        """Replace only absent optional installation and summary decoding.

        Main caller: portable CI. Real hooks, registration, global ownership,
        Run.flush/finish and journals stay intact; actual event conversion is
        independently required by the genuine-writer test below.
        """
        trace = []
        with patch.object(tb, "sync", side_effect=lambda run, directory, **kwargs:
                          trace.append((run.id, directory, kwargs["final"]))):
            for index, alias in enumerate(ALIASES):
                with self.subTest(alias=alias):
                    public = public_alias(alias)
                    self.assertIs(public.patch, tb.patch)
                    self.assertIs(public.unpatch, tb.unpatch)
                    root = Path(self.tmp.name) / str(index)
                    public.patch(save=False, root_logdir=str(root))
                    run = self.run_for("owner-" + str(index))
                    other = self.run_for("nonglobal-" + str(index), nonglobal=True)
                    paths = [str((root / name).resolve()) for name in ("train", "validation")]
                    for path in paths:
                        self.writer(path)
                    run.flush()
                    self.assertEqual(trace[-2:], [(run.id, path, False) for path in paths])
                    public.unpatch()
                    public_alias(ALIASES[1 - index]).unpatch()
                    self.assertIs(self.writer.__init__, self.original)
                    self.assertEqual(tb.directories(), [])
                    self.assertEqual(tb.directories(run), paths)
                    self.assertEqual(tb.directories(other), [])
                    for path in paths:
                        self.assertEqual(tb.source_options(path, run=run), (str(root.resolve()), False))
                        self.assertEqual(tb.source_options(path), (path, True))
                    self.writer(str(root / "unannounced"))
                    other.finish()
                    run.finish()
                    self.assertEqual([item for item in trace if item[0] == run.id and item[2]],
                                     [(run.id, path, True) for path in paths])
                    self.assertNotIn(run, tb._run_sources)
                    self.assertNotIn(other, tb._run_sources)
                    self.assertFalse(tb.is_patched())

    def test_retired_inference_and_new_session_are_separate(self):
        with patch.object(tb, "sync"):
            root = Path(self.tmp.name) / "old"
            tb.patch(save=False)
            old = self.run_for("inference-old")
            self.writer(str(root / "train"))
            with self.assertWarnsRegex(RuntimeWarning, "several event"):
                self.writer(str(root / "validation"))
            tb.unpatch()
            for name in ("train", "validation"):
                self.assertEqual(tb.source_options(root / name, run=old), (str(root.resolve()), False))
            # Making the already frozen inferred namespace explicit retains
            # identical behavior and must not trigger the changed-policy guard.
            tb.patch(save=False, root_logdir=str(root))
            self.writer(str(root / "train"))
            self.assertEqual(tb.source_options(root / "train", run=old), (str(root.resolve()), False))
            tb.unpatch()
            old.finish()
            new_root = Path(self.tmp.name) / "new"
            tb.patch(save=True, root_logdir=str(new_root))
            new = self.run_for("inference-new")
            self.assertEqual(tb.directories(new), [])
            self.writer(str(new_root / "only"))
            tb.unpatch()
            self.assertEqual(tb.directories(new), [str((new_root / "only").resolve())])
            self.assertEqual(tb.source_options(new_root / "only", run=new), (str(new_root.resolve()), True))
            new.finish()
            self.assertEqual(len(tb._run_sources), 0)

    def test_retry_after_unpatch_and_resume_do_not_restore_unrelated_watchers(self):
        root = Path(self.tmp.name) / "retry"
        tb.patch(save=False, root_logdir=str(root))
        run = self.run_for("retry")
        self.writer(str(root / "train"))
        tb.unpatch()
        calls, failed = [], False
        def decoder(owner, directory, *, final):
            nonlocal failed
            calls.append(final)
            if not failed:
                failed = True
                raise OSError("selected source temporarily unavailable")
            if final:
                owner.log({"drained": 1})
        with patch.object(tb, "sync", side_effect=decoder):
            with self.assertRaisesRegex(OSError, "temporarily unavailable"):
                run.flush()
            self.assertEqual(tb.directories(run), [str((root / "train").resolve())])
            run.flush()
            run.finish()
            run.finish()
        self.assertEqual(calls, [False, False, True])
        self.assertEqual(reduce_events(read_journal(run.sync_dir))["summary"]["drained"], 1)
        tb.patch(save=True, root_logdir=str(Path(self.tmp.name) / "fresh"))
        recovered = self.run_for("retry", resume="must")
        self.assertEqual(tb.directories(recovered), [])
        self.assertEqual(recovered.summary["drained"], 1)
        recovered.finish()
        tb.unpatch()

    def test_failed_final_import_releases_retired_sources(self):
        root = Path(self.tmp.name) / "failure"
        tb.patch(save=False, root_logdir=str(root))
        run = self.run_for("failure")
        self.writer(str(root / "train"))
        tb.unpatch()
        failure = ValueError("original final conversion failure")
        with patch.object(tb, "sync", side_effect=failure):
            with self.assertRaises(ValueError) as caught:
                run.finish()
        self.assertIs(caught.exception, failure)
        self.assertTrue(run._closed)
        self.assertFalse(run._tensorboard_registered)
        self.assertNotIn(run, tb._run_sources)
        self.assertNotIn(run, tb._registered_runs)
        self.assertFalse(tb.is_patched())

    def test_rejected_source_position_capture_precedes_fixture_only_cleanup(self):
        """Keep strict replay rejection separate from negative-probe teardown.

        Main caller: portable acceptance for the genuine TBX collision fixture.
        Source byte labels exercise actual Run.log/journal replay only; they do
        not replace or claim a genuine decoder. A rejected row stays pending,
        so this deliberately isolated fixture discards it after evidence capture
        rather than treating ordinary failed-handle finish as verified behavior.
        """
        from backprop.integration.tensorboard.grouping import closed_groups, history_metrics
        from backprop.integration.tensorboard.origin import event_origin
        run = self.run_for("rejected-position")
        filename = "events.out.tfevents.1.fixture.collision"
        header = {"origin": event_origin("train", filename, b"header"), "index": 0,
                  "step": "0", "wall_time": 100., "metrics": {}}
        first = {"origin": event_origin("train", filename, b"first-scalar"), "index": 1,
                 "step": "1", "wall_time": 101., "metrics": {"train/loss": .25}}
        run.log(history_metrics(list(closed_groups([header, first], final=True))[0]))
        before = read_journal(run.sync_dir)
        replaced = {**first, "origin": event_origin("train", filename, b"replacement-scalar"),
                    "metrics": {"train/loss": 99.}}
        with self.assertRaisesRegex(ValueError, "^Conflicting TensorBoard source position$"):
            run.log(history_metrics(list(closed_groups([header, replaced], final=True))[0]))
        self.assertEqual(read_journal(run.sync_dir), before)
        self.assertIn("_tensorboard_group", run._pending)
        self.assertEqual(run._pending["train/loss"], 99.)
        # Match only the negative probe's documented fixture cleanup. Ordinary
        # SDK finish with a rejected pending group is a separate lifecycle.
        with run._lock:
            run._pending.clear()
            run._pending_commits.clear()
        run.finish()
        history = reduce_events(read_journal(run.sync_dir))["history"]
        self.assertEqual([row["train/loss"] for row in history], [.25])

    def test_manual_global_binding_retains_sources_served_before_unpatch(self):
        """Preserve the existing native manual binding path across unpatch.

        Main caller: portable ownership regression. Pinned automatic hooks
        notify the global run at discovery time; manually binding a later
        create_new handle to poll an earlier discovery is a native behavior,
        not a claim that upstream retroactively notifies that second run.
        """
        root = Path(self.tmp.name) / "manual-binding"
        tb.patch(save=False, root_logdir=str(root))
        first = self.run_for("manual-first")
        second = self.run_for("manual-second", nonglobal=True)
        path = str((root / "train").resolve())
        self.writer(path)
        self.assertEqual(tb.directories(second), [])
        calls = []
        with patch.object(tb, "sync", side_effect=lambda run, directory, **kwargs:
                          calls.append((run.id, directory, kwargs["final"]))):
            second.flush()
            self.assertEqual(calls, [])
            backprop.run = second
            second.flush()
            self.assertEqual(calls, [(second.id, path, False)])
            self.assertEqual(list(tb._run_sources[second]), [path])
            # Already served streams keep their owner even if another handle
            # becomes global before the constructor hooks are removed.
            backprop.run = first
            tb.unpatch()
            tb.unpatch()
            self.assertEqual(tb.directories(second), [path])
            self.assertEqual(tb.source_options(path, run=second), (str(root.resolve()), False))
            second.finish()
            first.finish()
        self.assertEqual(calls, [(second.id, path, False), (second.id, path, True),
                                 (first.id, path, True)])
        self.assertNotIn(second, tb._run_sources)
        self.assertNotIn(first, tb._run_sources)

    def test_changed_policy_repatch_errors_without_mutating_live_source(self):
        """Reject the unsupported extra-watcher policy instead of ignoring it.

        Main caller: portable error regression for both selected aliases.
        Pinned can add a second watcher with a different policy for one path;
        native durable ownership currently represents only the original policy.
        Source identity remains stable; unchanged policy can still re-announce.
        """
        for index, alias in enumerate(ALIASES):
            with self.subTest(alias=alias):
                public = public_alias(alias)
                root = Path(self.tmp.name) / ("policy-" + str(index))
                path = str((root / "train").resolve())
                public.patch(save=False, root_logdir=str(root))
                run = self.run_for("policy-owner-" + str(index))
                self.writer(path)
                original = tb._run_sources[run][path]
                before = read_journal(run.sync_dir)
                initialized = list(self.writer_initializations)
                for save, selected_root in [(True, root), (False, root / "train")]:
                    public.unpatch()
                    public.patch(save=save, root_logdir=str(selected_root))
                    with self.assertRaisesRegex(backprop.UnsupportedFeatureError, "different root_logdir or save policy"):
                        self.writer(path)
                    self.assertEqual(tb.directories(), [])
                    self.assertEqual(tb._directory_options, {})
                    self.assertIs(tb._run_sources[run][path], original)
                    self.assertEqual(tb.source_options(path, run=run), (str(root.resolve()), False))
                    self.assertEqual(read_journal(run.sync_dir), before)
                    self.assertEqual(self.writer_initializations, initialized)
                public.unpatch()
                public.patch(save=False, root_logdir=str(root))
                self.writer(path)
                self.assertEqual(tb.directories(), [path])
                self.assertIs(tb._run_sources[run][path], original)
                calls = []
                with patch.object(tb, "sync", side_effect=lambda owner, directory, **kwargs:
                                  calls.append((directory, kwargs["final"]))):
                    public.unpatch()
                    run.finish()
                self.assertEqual(calls, [(path, True)])
                self.assertNotIn(run, tb._run_sources)


class TensorBoardUnpatchRealWriters(unittest.TestCase):
    @unittest.skipUnless(importlib.util.find_spec("tensorboard"), "Actual optional TensorBoard is unavailable")
    def test_pinned_and_native_live_streams_drain_after_unpatch_each_alias(self):
        """Require installed writers and actual core/journal outcomes per pair.

        Main caller: optional training CI. Raw rows, source bytes and process
        errors are retained before assertions; no source rows are filtered to
        hide multiplicity. Family dependency skips remain named and explicit.
        """
        reference = reference_runtime()
        for family, package in [("event", "tensorboard"), ("torch", "torch"), ("tensorboardx", "tensorboardX")]:
            for alias in ALIASES:
                with self.subTest(family=family, alias=alias):
                    if importlib.util.find_spec(package) is None:
                        self.skipTest("Actual optional writer is unavailable: " + package)
                    observations = {}
                    for mode in ("reference", "native"):
                        output = ROOT / "test-results" / ("tensorboard-gap-unpatch-" + family + "-" +
                            ("root" if alias == ALIASES[0] else "integration") + "-" + mode + "-raw.json")
                        env = {key: value for key, value in os.environ.items()
                               if not key.startswith(("WANDB_", "BACKPROP_", "FOUNDRY_"))}
                        paths = [ROOT / "tests"] if mode == "reference" else [ROOT / "sdk", ROOT / "tests"]
                        env.update(PYTHONPATH=os.pathsep.join(map(str, paths)), PYTHONDONTWRITEBYTECODE="1",
                                   WANDB_DATA_DIR="/tmp/backprop-reference-data")
                        result = run_observation([reference.python if mode == "reference" else sys.executable,
                            "-B", str(ROOT / "tests/tensorboard_gap_unpatch_probe.py"), mode, family, alias],
                            output, cwd=tempfile.gettempdir(), env=env, timeout=120)
                        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
                        observations[mode] = json.loads(output.read_text())
                    expected = [{"global_step": 1, "train/loss": .25},
                                {"global_step": 2, "train/loss": .75, "train/extra": 2.0},
                                {"global_step": 3, "train/loss": 1.0}]
                    for mode, data in observations.items():
                        self.assertEqual(data["selected_rows"], expected, mode)
                        self.assertEqual(data["nonglobal_rows"], [], mode)
                        self.assertTrue(all(data["aliases"].values()), mode)
                        self.assertTrue(data["constructor_restored"], mode)
                        self.assertEqual(data["patch_return"], None)
                        self.assertEqual(data["unpatch_returns"], [None, None])
                        self.assertTrue(data["source_objects"])
                        records = []
                        for source in data["source_objects"]:
                            frozen = base64.b64decode(source["base64"], validate=True)
                            self.assertEqual(len(frozen), source["bytes"])
                            self.assertEqual(hashlib.sha256(frozen).hexdigest(), source["sha256"])
                            if source["name"].startswith("train/"):
                                records.extend({key: record[key] for key in ["step", "wall_time", "metrics"]}
                                               for record in source["records"] if record["metrics"])
                        source_expected = [
                            {"step": 1, "wall_time": 101., "metrics": {"loss": .25}},
                            {"step": 2, "wall_time": 102., "metrics": {"loss": .5}},
                            {"step": 2, "wall_time": 102., "metrics": {"loss": .75, "extra": 2.0}},
                            {"step": 3, "wall_time": 103., "metrics": {"loss": 1.0}}]
                        if family != "event":
                            source_expected[2:3] = [
                                {"step": 2, "wall_time": 102., "metrics": {"loss": .75}},
                                {"step": 2, "wall_time": 102., "metrics": {"extra": 2.0}}]
                        self.assertEqual(records, source_expected, mode)
                        self.assertFalse(data["saved_tfevents"], mode)
                        self.assertEqual(data["unexpected_metric_rows"], [], mode)
                    native = observations["native"]
                    self.assertEqual(native["restart"]["read_failure"], "OSError")
                    self.assertEqual(native["restart"]["injected_read_calls"], 1)
                    self.assertTrue(native["restart"]["failed_read_preserved_history"])
                    self.assertTrue(native["restart"]["original_sources_unchanged"])
                    original_hashes = {source["name"]: source["sha256"] for source in native["source_objects"]}
                    self.assertEqual(native["restart"]["original_source_hashes"], original_hashes)
                    restarted = {source["name"]: source for source in native["restart"]["source_objects"]}
                    self.assertEqual({name: restarted[name]["sha256"] for name in original_hashes}, original_hashes)
                    new_names = sorted(set(restarted) - set(original_hashes))
                    self.assertEqual(native["restart"]["new_source_names"], new_names)
                    self.assertEqual(len(new_names), 1)
                    self.assertTrue(new_names[0].startswith("train/"))
                    self.assertTrue(new_names[0].endswith(".restart"))
                    for source in restarted.values():
                        frozen = base64.b64decode(source["base64"], validate=True)
                        self.assertEqual(len(frozen), source["bytes"])
                        self.assertEqual(hashlib.sha256(frozen).hexdigest(), source["sha256"])
                    self.assertEqual([record["metrics"] for record in restarted[new_names[0]]["records"] if record["metrics"]],
                                     [{"loss": 1.25}])
                    self.assertEqual(native["restart"]["selected_rows"], expected + [{"global_step": 4, "train/loss": 1.25}])
                    self.assertTrue(native["restart"]["finished_sources_released"])
                    self.assertEqual(native["restart"]["fresh_directories"], [])
                    if family == "tensorboardx":
                        collision = native["destructive_collision"]
                        self.assertEqual(collision["error"], {"type": "ValueError",
                            "message": "Conflicting TensorBoard source position"})
                        self.assertTrue(collision["failed_read_preserved_history"])
                        self.assertTrue(collision["rejected_pending_present"])
                        self.assertEqual(collision["fixture_cleanup"],
                            "Discard rejected pending row after evidence capture, release source, then finish; not failed-handle finish parity")
                        old, new = collision["before_source_objects"], collision["after_source_objects"]
                        self.assertEqual(len(old), 1)
                        self.assertEqual(len(new), 1)
                        self.assertEqual(old[0]["name"], new[0]["name"])
                        self.assertNotEqual(old[0]["sha256"], new[0]["sha256"])
                        for source in [*old, *new]:
                            frozen = base64.b64decode(source["base64"], validate=True)
                            self.assertEqual(len(frozen), source["bytes"])
                            self.assertEqual(hashlib.sha256(frozen).hexdigest(), source["sha256"])
                        self.assertEqual([record["metrics"] for record in old[0]["records"] if record["metrics"]],
                                         [{"loss": .25}, {"loss": .5}])
                        self.assertEqual([record["metrics"] for record in new[0]["records"] if record["metrics"]],
                                         [{"loss": 99.}])


if __name__ == "__main__":
    unittest.main()
