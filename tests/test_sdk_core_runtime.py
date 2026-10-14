"""Settings and branch contracts exercised through real persisted Run events.

Architecture: memory transport isolates behavior -> event assertions check the
observable contract -> subprocesses isolate native console redirection. Timing
tests use explicit gates, so they verify bounded waits without network traffic.
Main callers: the core SDK unittest suite.
"""
import contextlib
import importlib.util
import importlib
import io
import json
import logging
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from importlib import import_module
from unittest.mock import patch
import warnings

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sdk"))
import backprop as wandb
from backprop.branching import RunMoment


class RuntimeContracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.sink = wandb.MemorySink()
        self.runs = []
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        wandb.teardown()

    def tearDown(self):
        for run in self.runs:
            if not run._closed:
                run.finish()
        wandb.teardown()
        self.environment.stop()
        self.temp.cleanup()

    def new(self, *, settings=None, **kwargs):
        settings = {"x_disable_stats":True,"console": "off", "quiet": True, **(settings or {})}
        run = wandb.Run(project="runtime", dir=self.temp.name, settings=settings,
                        _sink=self.sink, _deployment={}, **kwargs)
        self.runs.append(run)
        return run

    def events(self, kind, run=None):
        return [event["payload"] for event in self.sink.events
                if event["type"] == kind and (run is None or event["run_id"] == run.id)]

    def test_tags_literal_comma_and_environment_split(self):
        self.assertEqual(wandb.Settings(run_tags="a,b").run_tags, ("a,b",))
        settings = wandb.Settings()
        settings.update_from_env_vars({"WANDB_TAGS": "a,b"})
        self.assertEqual(settings.run_tags, ("a", "b"))

    def test_branch_settings_validate_before_run_creation(self):
        for kwargs in ({"resume": "must", "resume_from": "run?_step=1"},
                       {"fork_from": "run?epoch=1"}, {"resume_from": "run?_step=nan"}):
            with self.subTest(kwargs=kwargs), self.assertRaises((ValueError, wandb.UsageError)):
                wandb.Settings(**kwargs)
        settings = wandb.Settings(fork_from="parent?_step=1.5")
        self.assertIsInstance(settings.fork_from, RunMoment)
        self.assertEqual(settings.fork_from.value, 1.5)

    def test_settings_run_directories_are_actual_paths(self):
        run = self.new()
        self.assertEqual(run.settings.files_dir, run.dir)
        self.assertEqual(run.settings.sync_dir, run.sync_dir)
        self.assertTrue(Path(run.settings.sync_file).is_file())

    def test_settings_file_snapshot_precedence_and_atomic_invalid_input(self):
        global_file = Path(self.temp.name) / "global-settings"
        workspace = Path(self.temp.name) / "wandb/settings"
        workspace.parent.mkdir()
        global_file.write_text("[default]\nproject=global\nquiet=true\n")
        workspace.write_text("[default]\nproject=local\n")
        settings = wandb.Settings(root_dir=self.temp.name, settings_system=str(global_file))
        snapshot = settings.read_system_settings()
        self.assertEqual(snapshot.all()["project"], "local")
        self.assertEqual(snapshot.sources, [global_file, workspace])
        settings.update_from_system_settings()
        self.assertEqual(settings.project, "local")
        self.assertTrue(settings.quiet)
        snapshot.set("project", "changed", globally=True)
        self.assertEqual(settings.project, "local")
        snapshot.save()
        self.assertEqual(settings.read_system_settings().all()["project"], "changed")
        workspace.write_text("[default]\nproject=would-be-partial\ninit_timeout=invalid\n")
        with warnings.catch_warnings(record=True) as seen:
            settings.update_from_system_settings()
        self.assertTrue(seen)
        self.assertEqual(settings.project, "local")

    def test_settings_none_updates_and_git_root_inference(self):
        settings = wandb.Settings(project="kept")
        settings.update_from_dict({"project": None})
        self.assertEqual(settings.project, "kept")
        root = Path(self.temp.name) / "repo"
        child = root / "child"
        child.mkdir(parents=True)
        (root / ".git").mkdir()
        settings.root_dir = str(child)
        self.assertIsNone(settings.infer_git_root())
        self.assertEqual(settings.git_root, str(root.resolve()))
        callback = threading.Event().set
        settings.stop_fn = callback
        self.assertIs(settings.model_copy(deep=True).stop_fn, callback)

    def test_nonfinite_initial_config_is_losslessly_persisted(self):
        run = self.new(config={"infinity": float("inf")})
        run.finish()
        self.assertEqual(self.events("run_start", run)[0]["config"]["infinity"], {"_type": "number", "value": "Infinity"})

    @unittest.skipUnless(importlib.util.find_spec("yaml"), "PyYAML optional config-file dependency unavailable")
    def test_config_paths_order_and_init_override(self):
        a, b = Path(self.temp.name) / "a.yaml", Path(self.temp.name) / "b.yaml"
        a.write_text("rate:\n  value: 1\nonly_a:\n  value: yes\n")
        b.write_text("rate:\n  value: 2\n")
        run = self.new(settings={"config_paths": [str(a), str(b)]}, config={"rate": 3})
        self.assertEqual(run.config.rate, 3)
        self.assertTrue(run.config.only_a)

    def test_file_ignore_patterns_filter_upload_without_mutation(self):
        source = Path(self.temp.name) / "private.txt"
        source.write_text("private")
        run = self.new(settings={"ignore_globs": ["*.txt"]})
        self.assertEqual(run.save(source, base_path=self.temp.name, policy="now"), [])
        run.finish()
        self.assertNotIn("private.txt", [row["name"] for row in self.events("file", run)])
        self.assertEqual(source.read_text(), "private")

    def test_wrap_capture_restores_stream_and_saves_console_file(self):
        target = io.StringIO()
        with contextlib.redirect_stdout(target):
            original = sys.stdout
            run = self.new(settings={"x_disable_stats":True,"console": "wrap_raw"})
            print("training output")
            run.finish()
            self.assertIs(sys.stdout, original)
            print("after finish")
        self.assertEqual(target.getvalue(), "training output\nafter finish\n")
        text = "".join(row["text"] for row in self.events("console", run))
        self.assertEqual(text, "training output\n")
        files = [row for row in self.events("file", run) if row["name"] == "output.log"]
        self.assertEqual(self.sink.download_file(files[-1]["file"]), b"training output\n")

    def test_capture_logger_independent_of_console_and_restored(self):
        logger = logging.getLogger("backprop-test-runtime")
        previous_level, previous_propagate = logger.level, logger.propagate
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        try:
            run = self.new(settings={"capture_loggers": {logger.name: "WARNING"}})
            logger.info("ignored")
            logger.warning("captured")
            run.finish()
            logger.warning("after finish")
            self.assertFalse(logger.handlers)
        finally:
            logger.setLevel(previous_level)
            logger.propagate = previous_propagate
        self.assertEqual([row["text"] for row in self.events("console", run)], ["WARNING:backprop-test-runtime:captured\n"])

    def test_multipart_console_uploads_closed_parts(self):
        run = self.new(settings={"console_multipart": True, "console_chunk_max_bytes": 5})
        run.write_logs("first")
        run.flush()
        first = [row for row in self.events("file", run) if row["name"].startswith("logs/")]
        self.assertEqual(len(first), 1)
        self.assertTrue(first[0]["name"].startswith("logs/output_"))
        run.write_logs("x")
        run.finish()
        self.assertEqual(len([row for row in self.events("file", run) if row["name"].startswith("logs/")]), 2)
        self.assertEqual(self.sink.download_file(first[0]["file"]), b"first\n")

    def test_console_writer_never_holds_console_lock_waiting_for_run(self):
        run = self.new(settings={"console_multipart": True, "console_chunk_max_bytes": 1})
        entered = threading.Event()
        underlying = run._lock
        class ObservedLock:
            def __enter__(self):
                entered.set()
                underlying.acquire()
                return self
            def __exit__(self, *args): underlying.release()
            def acquire(self, *args, **kwargs): return underlying.acquire(*args, **kwargs)
            def release(self): underlying.release()
            def _is_owned(self): return underlying._is_owned()
        run._lock = ObservedLock()
        run._lock.acquire()
        writer = threading.Thread(target=lambda: run.write_logs("concurrent"), daemon=True)
        writer.start()
        try:
            self.assertTrue(entered.wait(1))
            acquired = run._console.lock.acquire(timeout=0.2)
            self.assertTrue(acquired, "writer held console lock while blocked by the run lock")
            if acquired:
                run._console.lock.release()
        finally:
            run._lock.release()
            writer.join(1)
        self.assertFalse(writer.is_alive())
        original_poll = run._console.poll
        def checked_poll(*args, **kwargs):
            self.assertFalse(run._lock._is_owned(), "flush must poll console before acquiring its run lock")
            return original_poll(*args, **kwargs)
        run._console.poll = checked_poll
        run.flush()

    def test_console_redirect_captures_native_writes_in_subprocess(self):
        code = """import os, backprop as w
s=w.MemorySink()
r=w.init(project='native', dir=os.environ['TEST_DIR'], settings={'x_disable_stats':True,'console':'redirect'}, _sink=s, _deployment={})
os.write(1, b'native output\\n')
r.finish()
assert 'native output\\n' in ''.join(e['payload']['text'] for e in s.events if e['type']=='console')
"""
        result = subprocess.run([sys.executable, "-c", code], env={**os.environ,
                                "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "sdk"),
                                "TEST_DIR": self.temp.name}, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "native output\n")

    def test_finish_timeout_preserves_queue_and_raises_when_requested(self):
        run = self.new(settings={"finish_timeout": 0.03, "finish_timeout_raises": True})
        gate, started = threading.Event(), threading.Event()
        original = self.sink.publish
        def blocked(events):
            started.set()
            gate.wait(2)
            original(events)
        self.sink.publish = blocked
        run.log({"loss": 1})
        start = time.monotonic()
        try:
            with warnings.catch_warnings(record=True) as seen, self.assertRaises(TimeoutError):
                run.finish()
            self.assertTrue(seen)
            self.assertLess(time.monotonic()-start, 0.3)
            self.assertTrue(run._closed)
            self.assertTrue(started.is_set())
            self.assertGreater(run._journal.count, run._journal.ack)
        finally:
            gate.set()
            self.sink.publish = original
            run.flush()
        self.assertEqual(run._journal.count, run._journal.ack)

    def test_init_timeout_limits_slow_resume_lookup(self):
        gate = threading.Event()
        original = self.sink.read_events
        def blocked(**kwargs):
            gate.wait(2)
            return []
        self.sink.read_events = blocked
        start = time.monotonic()
        try:
            with self.assertRaises(TimeoutError):
                self.new(id="slow", resume="allow", settings={"init_timeout": 0.03})
            self.assertLess(time.monotonic()-start, 0.3)
        finally:
            gate.set()
            self.sink.read_events = original

    def test_fork_copies_inclusive_history_and_keeps_parent_unchanged(self):
        parent = self.new(id="parent", config={"lr": 0.1})
        parent.define_metric("loss", summary="min,max,mean")
        for loss in [5, 3, 1]:
            parent.log({"loss": loss})
        parent.finish()
        fork = self.new(id="child", settings={"fork_from": "parent?_step=1"})
        self.assertEqual(fork.starting_step, 2)
        self.assertEqual(fork.config.lr, 0.1)
        self.assertEqual(fork.summary.loss, {"min": 3, "max": 5, "mean": 4})
        self.assertFalse(fork.resumed)
        fork.log({"loss": 4})
        fork.finish()
        self.assertEqual([row["step"] for row in self.events("history", fork)], [0, 1, 2])
        self.assertEqual(len(self.events("history", parent)), 3)
        original_ids = {event["event_id"] for event in self.sink.events if event["run_id"] == parent.id}
        self.assertFalse(original_ids & {event["event_id"] for event in self.sink.events if event["run_id"] == fork.id})

    def test_rewind_truncation_survives_subsequent_resume(self):
        first = self.new(id="rewind")
        for value in [1, 2, 9]:
            first.log({"value": value})
        first.finish()
        rewind = self.new(settings={"resume_from": "rewind?_step=1"})
        self.assertEqual(rewind.starting_step, 2)
        self.assertEqual(rewind.summary.value, 2)
        rewind.log({"value": 3})
        rewind.finish()
        self.assertEqual(self.events("history_truncate", rewind)[0], {"step": 1, "summary": {"value": 2}})
        resumed = self.new(id="rewind", resume="must")
        self.assertEqual(resumed.starting_step, 3)
        self.assertEqual(resumed.summary.value, 3)

    def test_rewind_validates_destination_identity(self):
        with self.assertRaises(wandb.UsageError):
            self.new(id="different", settings={"resume_from": "source?_step=1"})
        with self.assertRaises(wandb.UsageError):
            self.new(id="source", settings={"fork_from": "source?_step=1"})

    def test_offline_resume_is_ignored_and_uses_a_distinct_journal(self):
        first = self.new(id="offline", mode="offline")
        first.log({"loss": 1})
        first.finish()
        with warnings.catch_warnings(record=True) as captured:
            second = self.new(id="offline", mode="offline", resume="must")
        self.assertTrue(captured)
        self.assertFalse(second.resumed)
        self.assertEqual(second.starting_step, 0)
        self.assertNotEqual(first.sync_dir, second.sync_dir)

    def test_config_artifact_is_retained_locally_and_serialized_as_reference(self):
        source = Path(self.temp.name) / "model.bin"
        source.write_bytes(b"model")
        artifact = wandb.Artifact("config-model", type="model")
        artifact.add_file(source)
        run = self.new(config={"model": artifact})
        self.assertIs(run.config.model, artifact)
        run.finish()
        wire = self.events("run_start", run)[0]["config"]["model"]
        self.assertEqual(wire["artifact_id"], artifact.id)
        self.assertEqual(wire["_type"], "artifact")
        other = self.new()
        with self.assertRaises(ValueError):
            other.config.update({"new": 1, "nested": {"model": artifact}})
        self.assertNotIn("new", other.config)

    def test_cross_project_used_artifact_retrieves_source_snapshot(self):
        source = Path(self.temp.name) / "weights.bin"
        source.write_bytes(b"weights")
        producer = wandb.Run(entity="team", project="producer", dir=self.temp.name,
                             settings={"x_disable_stats":True,"console": "off"}, _sink=self.sink, _deployment={})
        self.runs.append(producer)
        artifact = producer.log_artifact(source, name="weights", type="model")
        producer.finish()
        consumer = self.new(entity="team")
        consumer.use_artifact(artifact.qualified_name)
        consumer.finish()
        api = wandb.Api._from_store(self.sink, entity="team", project="runtime", config={})
        used = list(api.run(consumer.path).used_artifacts())
        self.assertEqual(len(used), 1)
        self.assertEqual(used[0].id, artifact.id)
        self.assertEqual(used[0].project, "producer")

    def test_settings_credentials_are_transport_only(self):
        token = "secret-for-transport-only-781234"
        module = importlib.import_module("backprop.run")
        with patch.object(module, "native_store", return_value=self.sink) as create:
            run = wandb.Run(project="credential", dir=self.temp.name,
                            settings={"api_key": token, "console": "off", "disable_git": True}, _deployment={})
        self.runs.append(run)
        self.assertEqual(create.call_args.kwargs["token"], token)
        run.finish()
        self.assertNotIn(token, repr(run.settings))
        self.assertNotIn(token, repr(run._deployment))
        self.assertNotIn(token, repr(self.sink.events))
        self.assertTrue(all(token.encode() not in contents for contents in self.sink.files.values()))
        self.assertTrue(all(token.encode() not in path.read_bytes() for path in Path(run.sync_dir).rglob("*") if path.is_file()))

    def test_metadata_code_git_controls_and_remote_credentials_redaction(self):
        run = self.new(settings={"git_remote_url": "https://user:private-token@git.example/repo", "git_commit": "abc123"})
        run.finish()
        environment = [row["values"]["environment"] for row in self.events("run_metadata", run) if "environment" in row["values"]][0]
        self.assertEqual(environment["git"]["remote"], "https://git.example/repo")
        private = self.new(settings={"disable_code": True, "disable_git": True, "program": "secret.py"})
        private.finish()
        environment = [row["values"]["environment"] for row in self.events("run_metadata", private) if "environment" in row["values"]][0]
        self.assertNotIn("git", environment)
        self.assertNotIn("program", environment)
        self.assertNotIn("codePath", environment)

    def test_save_code_obeys_code_dir_and_disable_code(self):
        code = Path(self.temp.name) / "code"
        code.mkdir()
        (code / "train.py").write_text("print('train')\n")
        (code / "private.txt").write_text("not source")
        run = wandb.init(project="runtime", dir=self.temp.name, save_code=True,
                         settings={"x_disable_stats":True,"console": "off", "code_dir": str(code)}, _sink=self.sink, _deployment={})
        run.finish()
        artifacts = self.events("artifact", run)
        self.assertEqual(len(artifacts), 1)
        self.assertEqual([file["name"] for file in artifacts[0]["files"]], ["train.py"])
        disabled = wandb.init(project="runtime", dir=self.temp.name, save_code=True,
                              settings={"x_disable_stats":True,"console": "off", "code_dir": str(code), "disable_code": True}, _sink=self.sink, _deployment={})
        disabled.finish()
        self.assertEqual(self.events("artifact", disabled), [])

    def test_model_bridge_and_pinned_config_metadata(self):
        source = Path(self.temp.name) / "weights.bin"
        source.write_bytes(b"weights")
        run = self.new(config={"model.card": "# Model", "lr": 0.1})
        self.assertIsNone(run.log_model(source, name="bridge"))
        run.flush()
        artifact = self.events("artifact", run)[0]
        directory = Path(self.temp.name) / "downloaded"
        directory.mkdir()
        copied = directory / source.name
        copied.write_bytes(b"weights")
        with patch("backprop.artifacts.Artifact.download", return_value=str(directory)):
            self.assertEqual(run.use_model("bridge:"+artifact["version"]), str(copied))
        run.pin_config_keys(["model.card", "lr"])
        self.assertEqual(run.config["_wandb"]["pinned_keys"], ["model.card", "lr"])
        run.mark_preempting()
        run.finish()
        values = [row["values"] for row in self.events("run_metadata", run)]
        self.assertIn({"pinned_config_keys": ["model.card", "lr"]}, values)
        self.assertIn({"preempting": True}, values)

    def test_run_context_exit_states_and_basic_properties(self):
        run = self.new(entity="team", group="group", job_type="training")
        self.assertEqual(run.path, f"team/runtime/{run.id}")
        self.assertEqual(run.group, "group")
        self.assertEqual(run.job_type, "training")
        self.assertGreater(run.start_time, 0)
        with self.assertRaisesRegex(ValueError, "training error"):
            with run:
                run.log({"loss": 1})
                raise ValueError("training error")
        self.assertEqual(self.events("run_finish", run)[0]["exit_code"], 1)
        self.assertFalse(run.display())

    def test_summary_source_callback_protocol_preserves_nested_paths(self):
        from backprop.summary import Summary
        value = {"model": {"score": 1}}
        summary = Summary(lambda: value)
        updates = []
        summary._set_update_callback(updates.append)
        summary.model.score = 2
        del summary.model.score
        self.assertEqual(updates[0].update[0].key, ("model", "score"))
        self.assertEqual(updates[0].update[0].value, 2)
        self.assertEqual(updates[1].remove[0].key, ("model", "score"))
        self.assertEqual(value["model"]["score"], 1)

    def test_notebook_display_collapsed_and_vscode_fallback(self):
        import html
        from types import ModuleType, SimpleNamespace
        shell = SimpleNamespace(config={"IPKernelApp": {"connection_file": "jupyter-kernel.json"}},
                                kernel=SimpleNamespace(shell=SimpleNamespace(user_ns={})))
        ipython, display_module = ModuleType("IPython"), ModuleType("IPython.display")
        ipython.get_ipython = lambda: shell
        outputs = []
        display_module.HTML = lambda value: value
        display_module.display = outputs.append
        run = wandb.Run(project="display", dir=self.temp.name, settings={"x_disable_stats":True,"console": "off"},
                        _sink=self.sink, _deployment={"app_url": "https://app.example.invalid/"})
        self.runs.append(run)
        with patch.dict(sys.modules, {"IPython": ipython, "IPython.display": display_module}):
            self.assertTrue(run.display(height=320, hidden=True))
            self.assertIn("<details><summary>", outputs[0])
            self.assertIn('height="320"', outputs[0])
            self.assertIn("&amp;run=", outputs[0])
            shell.kernel.shell.user_ns["__vsc_ipynb_file__"] = "training.ipynb"
            self.assertFalse(run.display())
            self.assertEqual(run.to_html(), html.escape(str(run)))
            shell.kernel.shell.user_ns.clear()
            shell.config = {}
            self.assertFalse(run.display())

    def test_summary_update_accepts_mapping_items_and_is_atomic(self):
        from types import MappingProxyType
        run = self.new()
        run.summary.update(MappingProxyType({"score": 0.8}))
        class ItemsOnly:
            def items(self): return [("loss", 0.2)]
        run.summary.update(ItemsOnly())
        self.assertEqual(run.summary.as_dict(), {"score": 0.8, "loss": 0.2})
        with self.assertRaises(TypeError):
            run.summary.update({"valid": 1, "invalid": object()})
        self.assertNotIn("valid", run.summary.keys())

    def test_summary_finished_run_and_unbound_subdict_match_source_lifecycle(self):
        from backprop.summary import SummarySubDict
        run = self.new()
        run.summary.loss = 1
        run.finish()
        with warnings.catch_warnings(record=True) as seen:
            self.assertEqual(run.summary.as_dict(), {})
        self.assertTrue(any("finished run" in str(item.message) for item in seen))
        unbound = SummarySubDict()
        self.assertEqual(unbound.keys(), [])
        with self.assertRaises(AttributeError):
            unbound.update({"x": 1})

    def test_disabled_artifact_methods_are_chainable_or_none_without_io(self):
        run = self.new(mode="disabled")
        artifact = run.log_artifact("missing/path", aliases=["unavailable"])
        self.assertIs(artifact.wait().get_path("missing").download(), artifact)
        self.assertIsNone(run.use_artifact("missing"))
        self.assertIsNone(run.use_model("missing"))
        self.assertIsNone(run.log_model("missing"))
        self.assertIsNone(run.status())
        run.log([("a", 1), ("b", 2)], step="ignored", commit=False)
        self.assertEqual(run.summary.a, 1)
        self.assertEqual(run.step, 0)
        run.finish()
        self.assertEqual(self.sink.events, [])
        self.assertFalse(list(Path(self.temp.name).rglob("*")))

    def test_nonfinite_aggregates_follow_pinned_go_numeric_rules_after_resume(self):
        run = self.new(id="nonfinite")
        run.define_metric("value", summary="min,max,mean,first,last")
        run.log({"value": 1})
        run.log({"value": float("inf")})
        self.assertEqual(run.summary.value.min, 1)
        self.assertEqual(run.summary.value.max, float("inf"))
        self.assertEqual(run.summary.value.mean, float("inf"))
        run.finish()
        resumed = self.new(id="nonfinite", resume="must")
        resumed.log({"value": float("nan")})
        self.assertTrue(math.isnan(resumed.summary.value.min))
        self.assertTrue(math.isnan(resumed.summary.value.max))
        self.assertTrue(math.isnan(resumed.summary.value.mean))
        self.assertEqual(resumed.summary.value.first, 1)
        self.assertTrue(math.isnan(resumed.summary.value.last))
        resumed.define_metric("zero", summary="min,max")
        resumed.log({"zero": 0.0})
        resumed.log({"zero": -0.0})
        self.assertEqual(math.copysign(1, resumed.summary.zero.min), -1)
        self.assertEqual(math.copysign(1, resumed.summary.zero.max), 1)
        resumed.finish()
        wire = self.events("history", resumed)[-1]["summary"]["value"]
        self.assertEqual(wire["min"], {"_type": "number", "value": "NaN"})

    def test_unsupported_runtime_settings_fail_before_persistence(self):
        for field, value in [("identity_token_file", "/private/token"),
                             ("organization", "company"), ("launch", True),
                             ("disable_job_creation", False),
                             ("sweep_id", "sweep")]:
            with self.subTest(field=field), self.assertRaisesRegex(wandb.UsageError, field):
                self.new(settings={field: value})
        self.assertFalse(list(Path(self.temp.name).rglob("events.jsonl")))
        with warnings.catch_warnings(record=True) as seen:
            run = self.new(settings={"show_emoji": True, "max_end_of_run_summary_metrics": 2, "summary_timeout": 5})
        self.assertEqual(len(seen), 1)
        self.assertTrue(all("no effect" in str(item.message) or "no remote wait" in str(item.message) for item in seen))

    def test_footer_limits_colors_emoji_warning_limit_and_silence(self):
        run = self.new(settings={"quiet": False, "show_colors": True, "show_emoji": True,
                                 "max_end_of_run_history_metrics": 1, "max_end_of_run_summary_metrics": 2,
                                 "summary_warnings": 1})
        run.log({"a": 1, "b": 2, "c": 3})
        run.log({"a": 2, "b": 3, "c": 4})
        with warnings.catch_warnings(record=True):
            run._warn("old-warning")
            run._warn("latest-warning")
        target = io.StringIO()
        with contextlib.redirect_stderr(target):
            run.finish()
        output = target.getvalue()
        self.assertIn("\x1b[36m", output)
        self.assertIn("✓", output)
        self.assertEqual(output.count("history "), 1)
        self.assertEqual(output.count("summary "), 2)
        self.assertIn("latest-warning", output)
        self.assertNotIn("old-warning", output)
        silent = self.new(settings={"silent": True})
        target = io.StringIO()
        with contextlib.redirect_stderr(target):
            silent.finish()
        self.assertEqual(target.getvalue(), "")
        plain = self.new(settings={"quiet": False, "show_colors": False, "show_emoji": False,
                                   "max_end_of_run_history_metrics": 0, "max_end_of_run_summary_metrics": 0})
        plain.log({"x": 1})
        target = io.StringIO()
        with contextlib.redirect_stderr(target):
            plain.finish()
        self.assertNotIn("\x1b", target.getvalue())
        self.assertNotIn("✓", target.getvalue())
        self.assertNotIn("history ", target.getvalue())
        self.assertNotIn("summary ", target.getvalue())

    def test_extreme_scalars_finish_without_losing_integer_precision(self):
        run = wandb.init(project="extremes", dir=self.temp.name, settings={"x_disable_stats":True,"console": "off", "quiet": False},
                         _sink=self.sink, _deployment={})
        self.runs.append(run)
        run.log({"huge": 10**400, "range": -1e308})
        run.log({"huge": 10**400+1, "range": 1e308})
        run.summary.extra = 10**500
        target = io.StringIO()
        with contextlib.redirect_stderr(target):
            run.finish()
        self.assertIsNone(wandb.run)
        self.assertEqual(self.events("history", run)[-1]["metrics"]["huge"], 10**400+1)
        self.assertIn("history range ▁█", target.getvalue())
        self.assertNotIn("history huge", target.getvalue())
        self.assertIn("summary extra: 1", target.getvalue())

    def test_presentation_error_propagates_after_global_and_debug_cleanup(self):
        run = wandb.init(project="output-error", dir=self.temp.name, settings={"x_disable_stats":True,"console": "off", "quiet": False},
                         _sink=self.sink, _deployment={})
        self.runs.append(run)
        class BrokenOutput(io.StringIO):
            def write(self, text): raise OSError("output unavailable")
        with contextlib.redirect_stderr(BrokenOutput()), self.assertRaisesRegex(OSError, "output unavailable"):
            run.finish()
        self.assertIsNone(wandb.run)
        self.assertTrue(run._closed)
        self.assertTrue(run._debug._closed)

    def test_idle_console_rollover_does_not_need_log_or_flush(self):
        from backprop.transport import read_journal
        run = self.new(mode="offline", settings={"console_multipart": True, "console_chunk_max_seconds": 1})
        persisted = threading.Event()
        original = run._files.save
        def observe(path, *args, **kwargs):
            result = original(path, *args, **kwargs)
            if Path(path).name.startswith("output_"):
                persisted.set()
            return result
        run._files.save = observe
        run.write_logs("only write")
        self.assertTrue(persisted.wait(2), "idle console part was never persisted")
        files = [event["payload"] for event in read_journal(run.sync_dir)
                 if event["type"] == "file" and event["payload"]["name"].startswith("logs/output_")]
        self.assertEqual(len(files), 1)
        self.assertIsNone(run._console.path)
        self.assertIsNone(run._console.rotation_timer)

    def test_terminal_color_preference_overrides_tty_detection(self):
        target = io.StringIO()
        try:
            with contextlib.redirect_stderr(target):
                wandb.termsetup(wandb.Settings(show_colors=True))
                wandb.termlog("\x1b[31mcolored\x1b[0m", prefix=False)
                wandb.termsetup(wandb.Settings(show_colors=False))
                wandb.termlog("\x1b[31mplain\x1b[0m", prefix=False)
        finally:
            wandb.termsetup(wandb.Settings())
        self.assertEqual(target.getvalue(), "\x1b[31mcolored\x1b[0m\nplain\n")

    def test_old_wandb_key_never_reaches_foundry_auth(self):
        from backprop.transport import native_store
        os.environ.update(WANDB_API_KEY="old-wandb-key", FOUNDRY_TOKEN="foundry-token")
        with warnings.catch_warnings(record=True) as seen, patch.object(import_module("backprop.run"), "native_store", return_value=self.sink) as factory:
            run = wandb.Run(project="auth", dir=self.temp.name, settings={"x_disable_stats":True,"console": "off"}, _deployment={})
            self.runs.append(run)
        self.assertTrue(any("Ignoring WANDB_API_KEY" in str(item.message) for item in seen))
        self.assertEqual(factory.call_args.kwargs["token"], "foundry-token")
        self.assertNotIn("old-wandb-key", repr(factory.call_args))
        with patch("backprop.foundry.FoundryStore", return_value=self.sink) as constructor:
            native_store({"host": "https://example.invalid", "stream_dataset_rid": "ri.test.dataset.test"})
        # None leaves the store's explicit BACKPROP_TOKEN/FOUNDRY_TOKEN lookup
        # in charge; the obsolete W&B key is never supplied as an override.
        self.assertIsNone(constructor.call_args.kwargs["token"])
        from backprop.foundry import FoundryStore
        configured = FoundryStore({"host": "https://example.invalid", "stream_dataset_rid": "ri.foundry.main.dataset.00000000-0000-4000-8000-2dd5ece68122"})
        self.assertEqual(configured.token, "foundry-token")
        explicit = wandb.Settings(api_key="explicit-foundry-token")
        with warnings.catch_warnings(record=True), patch.object(import_module("backprop.run"), "native_store", return_value=self.sink) as factory:
            second = wandb.Run(project="auth", dir=self.temp.name, settings=explicit, _deployment={})
            self.runs.append(second)
        self.assertEqual(factory.call_args.kwargs["token"], "explicit-foundry-token")

    def test_resumed_config_artifacts_rehydrate_same_and_cross_project(self):
        artifact = wandb.Artifact("weights", type="model")
        source = Path(self.temp.name) / "weights.bin"
        source.write_bytes(b"immutable-weights")
        artifact.add_file(source)
        owner = self.new(id="artifact-owner", config={"model": artifact, "infinite": float("inf")})
        owner.finish()
        resumed = self.new(id="artifact-owner", resume="must")
        self.assertIsInstance(resumed.config.model, wandb.Artifact)
        self.assertEqual(resumed.config.model.id, artifact.id)
        self.assertEqual(resumed.config.infinite, float("inf"))
        downloaded = resumed.config.model.get_path("weights.bin").download(root=str(Path(self.temp.name) / "restored"))
        self.assertEqual(Path(downloaded).read_bytes(), b"immutable-weights")
        resumed.finish()
        consumer = wandb.Run(project="consumer", id="consumer", dir=self.temp.name,
                             config={"model": artifact}, settings={"x_disable_stats":True,"console": "off"}, _sink=self.sink, _deployment={})
        self.runs.append(consumer)
        consumer.finish()
        consumer = wandb.Run(project="consumer", id="consumer", resume="must", dir=self.temp.name,
                             settings={"x_disable_stats":True,"console": "off"}, _sink=self.sink, _deployment={})
        self.runs.append(consumer)
        self.assertIsInstance(consumer.config.model, wandb.Artifact)
        self.assertEqual(consumer.config.model.project, "runtime")
        self.assertEqual(consumer.config.model.id, artifact.id)

    def test_config_artifact_uri_and_version_dict_resolve_immutable_objects(self):
        artifact = wandb.Artifact("uri-model", type="model")
        parent = self.new(entity="team")
        parent.log_artifact(artifact)
        parent.finish()
        run = self.new(config={"model": "wandb-artifact://" + artifact.qualified_name})
        self.assertEqual(run.config.model.id, artifact.id)
        run.config.update({"id_model": {"_type": "artifactVersion", "id": artifact.id}})
        self.assertEqual(run.config.id_model.id, artifact.id)
        with self.assertRaisesRegex(ValueError, "host"):
            run.config.update({"external": "wandb-artifact://https://api.wandb.ai/_id/anything"})
        with self.assertRaisesRegex(ValueError, "top-level"):
            run.config.update({"nested": {"model": "wandb-artifact://" + artifact.qualified_name}})
        self.assertNotIn("external", run.config)
        run.finish()
        used = self.events("artifact_use", run)
        self.assertEqual({row["artifact_id"] for row in used}, {artifact.id})

    def test_explicit_saved_notebook_is_captured_without_discovery(self):
        notebook = Path(self.temp.name) / "training.ipynb"
        notebook.write_text('{"cells":[],"nbformat":4,"nbformat_minor":5,"metadata":{}}')
        run = wandb.init(project="notebook", dir=self.temp.name, save_code=True,
                         settings={"x_disable_stats":True,"console": "off", "notebook_name": str(notebook)}, _sink=self.sink, _deployment={})
        self.runs.append(run)
        run.finish()
        artifacts = self.events("artifact", run)
        self.assertEqual(len(artifacts), 2)
        self.assertEqual(artifacts[-1]["files"][0]["name"], "training.ipynb")
        self.assertEqual(json.loads(self.sink.download_file(artifacts[-1]["files"][0])), json.loads(notebook.read_text()))

    def test_code_snapshot_defaults_and_exclusions_apply_to_staged_files(self):
        directory = Path(self.temp.name) / "source"
        directory.mkdir()
        for name in ("train.py", "secret.py", "requirements.txt"):
            (directory / name).write_text("fixture")
        run = self.new(settings={"ignore_globs": ["secret*"]})
        staged = Path(run.sync_dir) / "tmp/code"
        staged.mkdir(parents=True)
        (staged / "secret-notebook.ipynb").write_text("private")
        artifact = run.log_code(directory)
        self.assertEqual(list(artifact.manifest.entries), ["train.py"])

    def test_offline_fork_config_artifact_uses_verified_parent_journal_bytes(self):
        from backprop.transport import read_journal
        source = Path(self.temp.name) / "model.bin"
        source.write_bytes(b"offline-model")
        artifact = wandb.Artifact("offline-model", type="model")
        artifact.add_file(source)
        parent = self.new(id="offline-parent", mode="offline", config={"model": artifact})
        parent.log({"loss": 1})
        parent.finish()
        queued_identity = next(event["payload"]["artifact_id"] for event in read_journal(parent.sync_dir)
                               if event["type"] == "artifact")
        fork = self.new(id="offline-child", mode="offline", settings={"fork_from": "offline-parent?_step=0"})
        restored = fork.config.model
        self.assertIsInstance(restored, wandb.Artifact)
        downloaded = restored.get_path("model.bin").download(root=str(Path(self.temp.name) / "offline-restored"))
        self.assertEqual(Path(downloaded).read_bytes(), b"offline-model")
        self.assertEqual(restored.state, "PENDING")
        from backprop.config_artifacts import JournalArtifactStore
        store = JournalArtifactStore(parent.sync_dir, [], None)
        with self.assertRaisesRegex(wandb.Error, "escapes"):
            store.download_file({"_backprop_local_file": "../../model.bin"})
        with self.assertRaisesRegex(wandb.Error, "validation"):
            store.download_file({"_backprop_local_file": "events.jsonl", "size": 0, "sha256": "wrong"})
        import shutil
        shutil.rmtree(parent.sync_dir)
        self.assertEqual(artifact.id, queued_identity)
        downloaded = restored.get_path("model.bin").download(root=str(Path(self.temp.name) / "after-parent-removal"))
        self.assertEqual(Path(downloaded).read_bytes(), b"offline-model")
        fork.finish()
        fork._journal.flush()
        remote = wandb.Api._from_store(self.sink, project="runtime").run(f"runtime/{fork.id}")
        self.assertEqual(list(remote.logged_artifacts())[0].id, queued_identity)

    def test_resumed_incremental_table_continues_once_with_atomic_schema_checks(self):
        first = self.new(id="table-resume")
        table = wandb.Table(columns=["x"], data=[[1], [2]], log_mode="INCREMENTAL")
        first.log({"samples": table})
        table.add_data(3)
        first.log({"samples": table})
        first.finish()
        resumed = self.new(id="table-resume", resume="must")
        new = wandb.Table(columns=["x"], data=[[4]], log_mode="INCREMENTAL")
        original_id = new._id
        with self.assertRaises(TypeError):
            resumed.log({"samples": new, "invalid": object()})
        self.assertEqual(new._id, original_id)
        self.assertEqual(new._base_offset, 0)
        with self.assertRaises(ValueError):
            resumed.log({"samples": wandb.Table(columns=["different"], data=[[4]], log_mode="INCREMENTAL")})
        resumed.log({"samples": new})
        new.add_data(5)
        resumed.log({"samples": new})
        resumed.finish()
        descriptors = [row["metrics"]["samples"] for row in self.events("history", resumed)]
        self.assertEqual([row["row_offset"] for row in descriptors], [0, 2, 3, 4])
        self.assertEqual([cell for row in descriptors for cell in row["data"]], [[1], [2], [3], [4], [5]])
        self.assertEqual({row["table_id"] for row in descriptors}, {table._id})
        fresh = self.new()
        fresh.log({"samples": wandb.Table(columns=["x"], data=[[9]], log_mode="INCREMENTAL")})
        fresh.finish()
        self.assertNotEqual(self.events("history", fresh)[0]["metrics"]["samples"]["table_id"], table._id)

    def test_redefining_metric_restores_aggregate_after_explicit_summary(self):
        run = self.new()
        run.define_metric("loss", summary="min,max")
        run.log({"loss": 4})
        run.summary.loss = 99
        run.log({"loss": 3})
        self.assertEqual(run.summary.loss, 3)
        run.define_metric("loss", summary="mean")
        run.log({"loss": 2})
        self.assertEqual(run.summary.loss, {"min": 2, "max": 4, "mean": 3})
        run.define_metric("none", summary="none")
        run.define_metric("none", summary="max")
        run.log({"none": 5})
        self.assertNotIn("none", run.summary.keys())
        run.define_metric("none", summary="max", overwrite=True)
        run.log({"none": 6})
        self.assertEqual(run.summary.none, {"max": 6})

    def test_nested_custom_axis_is_synchronized_without_duplicating_literal_keys(self):
        run = self.new()
        run.define_metric("train.loss", step_metric="train.epoch")
        run.log({"train": {"epoch": 4}})
        run.log({"train": {"loss": 0.5}})
        run.finish()
        metrics = self.events("history", run)[-1]["metrics"]
        self.assertEqual(metrics, {"train": {"loss": 0.5, "epoch": 4}})

    def test_import_aliases_share_runtime_classes_and_public_mappings(self):
        from backprop.compatibility import install_wandb_aliases
        install_wandb_aliases()
        from wandb.sdk.wandb_run import Run
        from wandb.sdk.wandb_config import Config
        from wandb.sdk.data_types.classes import Classes
        from wandb.apis.public import Api
        self.assertIs(Run, wandb.Run)
        self.assertIs(Config, wandb.Config)
        self.assertIs(Classes, wandb.Classes)
        self.assertIs(Api, wandb.Api)
        run = wandb.init(mode="disabled", config={"x": 1})
        self.assertIs(wandb.config, run.config)
        self.assertIs(wandb.summary, run.summary)
        run.finish()

    def test_background_retry_continues_local_logging_and_recovers(self):
        run = self.new(settings={"flush_interval": 0.01})
        attempts, failed = 0, threading.Event()
        original = self.sink.publish
        def temporarily_unavailable(events):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                failed.set()
                raise ConnectionError("temporary")
            return original(events)
        self.sink.publish = temporarily_unavailable
        with warnings.catch_warnings(record=True) as seen:
            run.log({"x": 1})
            self.assertTrue(failed.wait(1))
            run.log({"x": 2})
            deadline = time.monotonic()+1
            while len(self.events("history", run)) < 2 and time.monotonic() < deadline:
                time.sleep(0.01)
        self.assertEqual(len(self.events("history", run)), 2)
        self.assertTrue(seen)
        self.assertTrue(run._thread.is_alive())

    def test_stop_on_fatal_error_invokes_stop_function_and_retains_queue(self):
        stopped = threading.Event()
        run = self.new(settings={"flush_interval": 0.01, "stop_on_fatal_error": True, "stop_fn": stopped.set})
        original = self.sink.publish
        self.sink.publish = lambda events: (_ for _ in ()).throw(ValueError("Invalid event rejected permanently"))
        try:
            with warnings.catch_warnings(record=True):
                run.log({"x": 1})
                self.assertTrue(stopped.wait(1))
            with self.assertRaises(wandb.BackpropError):
                run.log({"x": 2})
            self.assertGreater(run._journal.count, run._journal.ack)
        finally:
            self.sink.publish = original
            run.flush()

    def test_terminal_emulator_handles_progress_unicode_and_split_sequences(self):
        from backprop.terminal import Terminal
        terminal = Terminal()
        rendered = ""
        for chunk in ["😀 progress 1", "\r😀 progress 2\x1b[", "K\n", "wrong", "\b\b\b\b\bright\n", "\x1b[31mred\x1b[0m\n"]:
            remove, text = terminal.feed(chunk)
            rendered = (rendered[:-remove] if remove else rendered) + text
        self.assertEqual(rendered, "😀 progress 2\nright\n\x1b[31mred\x1b[0m\n")

    def test_terminal_emulator_cursor_and_erase_operations(self):
        from backprop.terminal import Terminal
        terminal = Terminal()
        terminal.feed("one\ntwo\nthree")
        remove, text = terminal.feed("\x1b[1A\r\x1b[2Knew\x1b[1B\r\x1b[0Kdone")
        rendered = "one\ntwo\nthree"
        rendered = (rendered[:-remove] if remove else rendered)+text
        self.assertEqual(rendered, "one\nnew\ndone")
        remove, text = terminal.feed("\x1b[2J\x1b[Hclean")
        rendered = (rendered[:-remove] if remove else rendered)+text
        self.assertEqual(rendered.rstrip("\n"), "clean")

    def test_terminal_insert_lines_matches_pinned_renderer(self):
        from backprop.terminal import Terminal
        terminal = Terminal()
        _, rendered = terminal.feed("one\ntwo\nthree")
        remove, text = terminal.feed("\x1b[A\x1b[2L")
        rendered = (rendered[:-remove] if remove else rendered)+text
        self.assertEqual(rendered, "one\ntwo\n\n\nthree")

    def test_wrap_emu_file_and_console_edits_preserve_other_stream(self):
        run = self.new(settings={"x_disable_stats":True,"console": "wrap_emu"})
        run.log_console("progress 1", "stdout")
        run.log_console("warning\n", "stderr")
        run.log_console("\rprogress 2\x1b[K\n", "stdout")
        run.finish()
        events = self.events("console", run)
        self.assertEqual(events[-1]["replace_tail"], 1)
        files = [row for row in self.events("file", run) if row["name"] == "output.log"]
        self.assertEqual(self.sink.download_file(files[-1]["file"]), b"progress warning\n2\n")

    def test_dirty_worktree_patch_respects_save_code_and_upload_exclusions(self):
        repo = Path(self.temp.name) / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        script = repo / "train.py"
        script.write_text("value = 1\n")
        subprocess.run(["git", "-C", str(repo), "add", "train.py"], check=True)
        subprocess.run(["git", "-C", str(repo), "-c", "user.name=SDK test", "-c", "user.email=sdk@example.invalid", "commit", "-qm", "initial"], check=True)
        script.write_text("value = 2\n")
        run = self.new(settings={"save_code": True, "git_root": str(repo)})
        run.finish()
        patches = [row for row in self.events("file", run) if row["name"] == "diff.patch"]
        self.assertEqual(len(patches), 1)
        data = self.sink.download_file(patches[0]["file"]).decode()
        self.assertIn("-value = 1\n+value = 2", data)
        excluded = self.new(settings={"save_code": True, "git_root": str(repo), "ignore_globs": ["*.patch"]})
        excluded.finish()
        self.assertFalse([row for row in self.events("file", excluded) if row["name"].endswith(".patch")])
        no_code = self.new(settings={"save_code": True, "disable_code": True, "git_root": str(repo)})
        no_code.finish()
        self.assertFalse([row for row in self.events("file", no_code) if row["name"].endswith(".patch")])

    def test_offline_branch_missing_parent_defers_resolution_until_sync(self):
        with self.assertWarnsRegex(RuntimeWarning, "sync will resolve the parent"):
            run = wandb.Run(project="runtime", dir=self.temp.name, mode="offline",
                settings={"x_disable_stats":True,"console": "off", "quiet": True, "fork_from": "missing?_step=3"}, _deployment={})
            self.runs.append(run)
        self.assertEqual(run.step, 4)
        self.assertTrue((Path(run.sync_dir) / "branch-intent.json").is_file())
        run.finish()

    def test_quiet_silent_and_terminal_level_controls(self):
        target = io.StringIO()
        try:
            with contextlib.redirect_stderr(target):
                wandb.termsetup(wandb.Settings(quiet=True))
                wandb.termlog("hidden-info")
                wandb.termwarn("visible-warning")
                wandb.termsetup(wandb.Settings(silent=True))
                wandb.termerror("hidden-error")
                wandb.termsetup(wandb.Settings(show_info=False, show_warnings=False, show_errors=False))
                wandb.termlog("hidden-info2")
                wandb.termwarn("hidden-warning")
                wandb.termerror("hidden-error2")
        finally:
            wandb.termsetup(wandb.Settings())
        self.assertEqual(target.getvalue(), "wandb: WARNING visible-warning\n")

    @unittest.skipUnless(importlib.util.find_spec("torch"), "PyTorch optional array dependency unavailable")
    def test_torch_summary_detaches_gradient_tensor_and_preserves_dtype(self):
        import numpy as np
        import torch
        run = self.new()
        tensor = torch.arange(40, dtype=torch.float64, requires_grad=True).reshape(5, 8)
        run.summary.weights = tensor
        run.finish()
        value = self.events("summary", run)[0]["values"]["weights"]
        self.assertEqual(value["_type"], "torch.Tensor")
        self.assertEqual(value["dtype"], "float64")
        restored = np.load(io.BytesIO(self.sink.download_file(value["file"])), allow_pickle=False)
        np.testing.assert_array_equal(restored, tensor.detach().numpy())

    @unittest.skipUnless(importlib.util.find_spec("numpy"), "NumPy optional array dependency unavailable")
    def test_large_summary_array_statistics_and_lossless_binary(self):
        import numpy as np
        run = self.new()
        array = np.arange(40, dtype=np.float32).reshape(5, 8)
        run.summary.weights = array
        run.finish()
        value = self.events("summary", run)[0]["values"]["weights"]
        self.assertEqual(value["_type"], "numpy.ndarray")
        self.assertEqual(value["shape"], [5, 8])
        self.assertEqual(value["mean"], 19.5)
        np.testing.assert_array_equal(np.load(io.BytesIO(self.sink.download_file(value["file"])), allow_pickle=False), array)


if __name__ == "__main__":
    unittest.main()
