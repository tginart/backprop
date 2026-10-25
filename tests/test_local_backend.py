"""
Purpose: Exercise the local backend through real SDK and HTTP boundaries.
Input: Temporary databases, journals, artifacts, and loopback servers.
Output: Assertions for durability, retries, concurrency, and SDK compatibility.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib import request

import backprop
from backprop.foundry import FoundryStore
from backprop.media import EncodedMedia
from backprop_backend.http_store import BackendError
from backprop.local_config import local_config, normalize_local_config
from backprop_backend.local.database import LocalDatabase
from backprop_backend.local.server import _Handler, create_server, running_server
from backprop.local_store import LocalStore
from backprop.transport import BackpropError, Journal, native_store, sync


def event(identifier: str, *, step: int = 0) -> dict:
    return {
        "schema_version": 1, "event_id": identifier, "run_id": "run-1",
        "project": "project", "entity": "local", "type": "history",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "payload": {"step": step, "metrics": {"loss": step + 0.25}},
    }


class LocalBackendTests(unittest.TestCase):
    """
    Purpose: Verify the real local server without a Foundry network connection.
    Input: One isolated server and temporary source directories per test.
    Output: End-to-end storage and SDK assertions.
    """

    def setUp(self) -> None:
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.server = self.stack.enter_context(running_server(self.root / "database"))
        self.store = LocalStore(self.server.connection)
        self.stack.enter_context(patch.dict("os.environ", {
            "BACKPROP_CONFIG": str(self.server.database.directory / "connection.json"),
            "BACKPROP_TOKEN": "unused-foundry-token", "FOUNDRY_TOKEN": "unused-token",
        }))
        self.stack.enter_context(patch.object(backprop, "_setup_settings", None))

    def test_query_cache_expires_without_changes_to_events(self) -> None:
        """
        Purpose: Bound query metadata while the experiment data stays unchanged.
        Input: Repeated history queries separated by the snapshot retry window.
        Output: Only current query metadata remains, and old IDs expire.
        """
        self.store.publish([event("cached-history")])
        query = f"SELECT * FROM `{self.store.stream_rid}` ORDER BY timestamp LIMIT 100"
        with patch("time.time", return_value=10000):
            old_id = self.server.database.execute_query(query)
            expected = self.server.database.query_result(old_id)
        with patch("time.time", return_value=13599):
            restored = LocalDatabase(self.server.database.directory)
            self.assertEqual(restored.query_result(old_id), expected)
        for stamp in (13600, 17200, 20800):
            with patch("time.time", return_value=stamp):
                current = self.server.database.execute_query(query)
                self.assertEqual(self.server.database.query_result(current), expected)
                with self.server.database.connect() as connection:
                    self.assertEqual(connection.execute("SELECT COUNT(*) FROM queries").fetchone()[0], 1)
                with self.assertRaises(FileNotFoundError):
                    self.server.database.query_result(old_id)
        self.assertEqual(self.server.database.end_offset(), 1)

    def test_query_cancel_releases_snapshot_and_is_safe_to_repeat(self) -> None:
        query = self.store.request("/api/v2/sqlQueries/execute", "POST", {
            "query": f"SELECT * FROM `{self.store.stream_rid}` ORDER BY timestamp LIMIT 100",
            "serializationFormat": "CSV", "fallbackBranchIds": ["master"],
        })
        path = "/api/v2/sqlQueries/" + query["queryId"]
        self.store.request(path + "/cancel", "POST")
        self.store.request(path + "/cancel", "POST")
        with self.assertRaises(BackendError) as missing:
            self.store.request(path + "/getResults")
        self.assertEqual(missing.exception.status_code, 404)

    def test_version_one_queries_keep_a_retry_window_after_schema_upgrade(self) -> None:
        """
        Purpose: Preserve existing query IDs through the cache schema upgrade.
        Input: A version-one database with an immutable query snapshot.
        Output: The old query works during the retry window and then expires.
        """
        directory = self.root / "legacy"
        directory.mkdir()
        snapshot = {"sql": "SELECT schema_version,event_id,run_id,project,entity,event_type,timestamp,payload FROM events WHERE offset<=? ORDER BY timestamp LIMIT ?",
                    "parameters": [0, 100]}
        with sqlite3.connect(directory / "backprop.sqlite3") as connection:
            connection.execute("CREATE TABLE queries (id TEXT PRIMARY KEY, result BLOB NOT NULL)")
            connection.execute("INSERT INTO queries VALUES (?,?)", ("legacy", json.dumps(snapshot).encode()))
            connection.execute("PRAGMA user_version=1")
        with patch("time.time", return_value=10000):
            database = LocalDatabase(directory)
            result, end = database.query_page("legacy")
            self.assertIn(b"event_id", result)
            self.assertEqual(end, 0)
        with patch("time.time", return_value=13599):
            self.assertEqual(LocalDatabase(directory).query_result("legacy"), result)
        with patch("time.time", return_value=13600):
            self.assertFalse(database.has_query("legacy"))
            with self.assertRaises(FileNotFoundError):
                database.query_result("legacy")

    def test_query_boundary_is_stable_and_status_does_not_render_csv(self) -> None:
        self.store.publish([event("before-snapshot")])
        query = self.store.request("/api/v2/sqlQueries/execute", "POST", {
            "query": f"SELECT * FROM `{self.store.stream_rid}` ORDER BY timestamp LIMIT 100",
            "serializationFormat": "CSV", "fallbackBranchIds": ["master"],
        })
        path = "/api/v2/sqlQueries/" + query["queryId"]
        self.store.publish([event("after-snapshot")])
        with patch.object(self.server.database, "query_page", side_effect=AssertionError("CSV scan")):
            self.assertEqual(self.store.request(path + "/getStatus")["type"], "succeeded")
        for _ in range(2):
            response = request.urlopen(self.server.origin + path + "/getResults")
            with response:
                self.assertEqual(response.headers["X-Backprop-End-Offset"], "1")
                content = response.read()
                self.assertIn(b"before-snapshot", content)
                self.assertNotIn(b"after-snapshot", content)

    def test_scoped_local_reads_do_not_fetch_unrelated_stream_records(self) -> None:
        """
        Purpose: Use the complete local archive for a scoped SDK read.
        Input: Unrelated runs and one requested run in the same installation.
        Output: The SDK returns only the requested rows without a live stream scan.
        """
        unrelated = [{**event(f"other-{i}"), "project": "other"} for i in range(1100)]
        for offset in range(0, len(unrelated), 500):
            self.store.publish(unrelated[offset:offset + 500])
        expected = event("requested")
        self.store.publish([expected])
        with patch.object(self.store, "read_live_records", side_effect=AssertionError("Unrelated live replay")):
            self.assertEqual(self.store.read_events(project="project", run_id="run-1", entity="local"), [expected])

    def test_identical_replay_keeps_offsets_and_conflict_rolls_back_batch(self) -> None:
        original = event("original")
        self.store.publish([original, original])
        self.assertEqual(self.server.database.end_offset(), 1)
        conflict = {**original, "payload": {"different": True}}
        with self.assertRaises(BackendError) as raised:
            self.store.publish([event("must-rollback"), conflict])
        self.assertEqual(raised.exception.status_code, 409)
        self.assertFalse(raised.exception.retryable)
        self.assertEqual(self.server.database.end_offset(), 1)
        self.assertEqual(self.store.read_events(), [original])

    def test_workspace_revisions_stay_durable_and_outside_experiment_replay(self) -> None:
        """
        Purpose: Keep display revisions durable without changes to experiment replay.
        Input: A normal event and a workspace revision in the same event stream.
        Output: Both raw records persist, and SDK replay returns only the experiment.
        """
        original = event("history-before-workspace")
        display = {**event("workspace-revision"), "type": "workspace_snapshot",
                   "run_id": "__workspace__:test", "payload": {"snapshot": {}}}
        self.store.publish([original, display])
        self.assertEqual(len(list(self.store.read_live_records())), 2)
        self.assertEqual(self.store.read_events(), [original])
        database = LocalDatabase(self.server.database.directory)
        self.assertEqual(database.end_offset(), 2)

    def test_restart_keeps_offsets_artifacts_claims_and_query_snapshot(self) -> None:
        """
        Purpose: Verify restart keeps offsets artifacts claims and query snapshot.
        Input: Isolated test data and the real backend interfaces.
        Output: Assertions for the documented storage contract.
        """
        directory = self.root / "restart"
        source = self.root / "weights.bin"
        source.write_bytes(b"model-weights")
        first = event("first")
        with running_server(directory) as server:
            store = LocalStore(server.connection)
            identifier = store.config["local_id"]
            store.publish([first])
            reference = store.upload_file(source)
            key = "a" * 64
            store.claim_artifact_resolution(key, {"winner": 1})
            query_id = server.database.execute_query(
                f"SELECT * FROM `{store.stream_rid}` ORDER BY timestamp LIMIT 100"
            )
        with running_server(directory) as server:
            store = LocalStore(server.connection)
            self.assertEqual(store.config["local_id"], identifier)
            self.assertEqual(store.read_events(), [first])
            self.assertEqual(store.download_file(reference), source.read_bytes())
            self.assertEqual(store.claim_artifact_resolution(key, {"winner": 2}), {"winner": 1})
            store.publish([event("second")])
            self.assertEqual(server.database.end_offset(), 2)
            self.assertNotIn(b"second", server.database.query_result(query_id))
            rows = server.database.read_records(start=1, limit=10)
            self.assertEqual([row["offset"] for row in rows], ["1"])

    def test_stopped_backup_restores_offsets_identity_and_artifact_bytes(self) -> None:
        """
        Purpose: Verify the documented stopped-directory backup and restore procedure.
        Input: A committed event and artifact in an isolated local installation.
        Output: The same identity and data, plus a valid next stream offset.
        """
        import shutil
        source = self.root / 'backup-source'
        artifact = self.root / 'backup-artifact.txt'
        artifact.write_bytes(b'backup artifact bytes')
        original = event('before-backup')
        with running_server(source) as server:
            store = LocalStore(server.connection)
            store.publish([original])
            reference = store.upload_file(artifact)
            identity = store.config['local_id']
            offsets = store.request(store.stream_path + '/getEndOffsets?' + store._stream_query())
        restored = self.root / 'restored-installation'
        shutil.copytree(source, restored)
        with running_server(restored) as server:
            store = LocalStore(server.connection)
            self.assertEqual(store.config['local_id'], identity)
            self.assertEqual(store.read_events(), [original])
            self.assertEqual(store.download_file(reference), artifact.read_bytes())
            self.assertEqual(store.request(store.stream_path + '/getEndOffsets?' + store._stream_query()), offsets)
            store.publish([event('after-backup')])
            updated = store.request(store.stream_path + '/getEndOffsets?' + store._stream_query())
            self.assertEqual(int(updated['0']), int(offsets['0']) + 1)

    def test_interrupted_request_body_does_not_commit(self) -> None:
        body = json.dumps({"records": [self.store.event_to_record(event("partial"))]}).encode()
        with socket.create_connection(self.server.server_address) as client:
            headers = (
                f"POST {self.store.stream_path}/publishRecords HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{self.server.server_port}\r\n"
                "Content-Type: application/json\r\n"
                f"Content-Length: {len(body)}\r\n\r\n"
            ).encode()
            client.sendall(headers + body[:len(body) // 2])
            client.shutdown(socket.SHUT_WR)
            self.assertIn(b"400", client.recv(4096))
        self.assertEqual(self.server.database.end_offset(), 0)

    def test_lost_commit_response_leaves_journal_retryable(self) -> None:
        """
        Purpose: Verify lost commit response leaves journal retryable.
        Input: Isolated test data and the real backend interfaces.
        Output: Assertions for the documented storage contract.
        """
        journal = Journal(self.root / "journal", sink=self.store)
        journal.append(event("lost-response"))
        send = _Handler._send

        def disconnect(handler, data, **kwargs):
            if "publishRecords" in handler.path:
                self.assertEqual(handler.server.database.end_offset(), 1)
                self.assertEqual(journal.ack, 0)
                handler.connection.shutdown(socket.SHUT_RDWR)
                handler.close_connection = True
                return
            return send(handler, data, **kwargs)

        with patch.object(_Handler, "_send", disconnect), patch("backprop.foundry.time.sleep"):
            with self.assertRaises(BackpropError):
                journal.flush()
        self.assertEqual(journal.ack, 0)
        self.assertFalse(journal.ack_path.exists())
        journal.flush()
        self.assertEqual(journal.ack, 1)
        self.assertEqual(self.server.database.end_offset(), 1)
        self.assertNotEqual(journal.ack_path.name, "ack.json")

    def test_concurrent_writers_readers_and_first_artifact_claim(self) -> None:
        """
        Purpose: Verify concurrent writers readers and first artifact claim.
        Input: Isolated test data and the real backend interfaces.
        Output: Assertions for the documented storage contract.
        """
        records = [event(f"parallel-{index}", step=index) for index in range(30)]
        barrier = threading.Barrier(6)
        key = "b" * 64

        def writer(index):
            store = LocalStore(self.server.connection)
            barrier.wait()
            for record in records:
                store.publish([record])
                snapshot = store.read_events()
                self.assertEqual(len({row["event_id"] for row in snapshot}), len(snapshot))
            return store.claim_artifact_resolution(key, {"writer": index})

        with ThreadPoolExecutor(max_workers=6) as pool:
            choices = list(pool.map(writer, range(6)))
        self.assertTrue(all(choice == choices[0] for choice in choices))
        self.assertEqual(self.server.database.end_offset(), len(records))
        self.assertEqual(len(self.store.read_events()), len(records))

    def test_import_preserves_source_foundry_ack_and_resolution_state(self) -> None:
        """
        Purpose: Verify import preserves source foundry ack and resolution state.
        Input: Isolated test data and the real backend interfaces.
        Output: Assertions for the documented storage contract.
        """
        source = self.root / "original"
        journal = Journal(source)
        staged = journal.stage_file(None, data=b"durable-artifact", name="weights.bin")
        journal.append({**event("import-file"), "payload": {"file": staged}})
        ack = source / "ack.json"
        ack.write_text('{"count":1,"byte_offset":1}')
        cache = source / "incremental-artifacts"
        cache.mkdir()
        (cache / "source.json").write_text('{"foundry":"unchanged"}')
        original = {str(path.relative_to(source)): path.read_bytes()
                    for path in source.rglob("*") if path.is_file()}
        self.assertEqual(sync(source, config=self.server.connection), 1)
        self.assertEqual(sync(source, config=self.server.connection), 1)
        self.assertEqual(self.server.database.end_offset(), 1)
        self.assertEqual(original, {str(path.relative_to(source)): path.read_bytes()
                                   for path in source.rglob("*") if path.is_file()})
        reference = self.store.read_events()[0]["payload"]["file"]
        self.assertEqual(self.store.download_file(reference), b"durable-artifact")
        ledger = hashlib.sha256(str(source.resolve()).encode()).hexdigest()
        self.assertEqual(self.server.database.import_state(ledger)["count"], 1)
        journal.append(event("later"))
        self.assertEqual(sync(source, config=self.server.connection), 2)
        self.assertEqual(self.server.database.end_offset(), 2)

    def test_import_waits_for_complete_lines_and_detects_changed_prefix(self) -> None:
        source = self.root / "partial-source"
        journal = Journal(source)
        first = event("complete")
        journal.append(first)
        with journal.event_path.open("ab") as output:
            output.write(b'{"incomplete":')
        self.assertEqual(self.store.import_journal(source), 1)
        self.assertEqual(self.server.database.end_offset(), 1)
        journal.event_path.write_text(json.dumps(event("changed")) + "\n")
        with self.assertRaisesRegex(ValueError, "prefix|changed"):
            self.store.import_journal(source)

    def test_cli_import_keeps_a_running_servers_connection_file(self) -> None:
        """
        Purpose: Import beside a live server without a connection-file change.
        Input: A complete journal and the active installation directory.
        Output: Committed events and the original live server connection.
        """
        source = self.root / "cli-import"
        journal = Journal(source)
        original = event("cli-import")
        journal.append(original)
        connection = self.server.database.directory / "connection.json"
        before = connection.read_bytes()
        result = subprocess.run(
            [sys.executable, "-B", "-m", "backprop", "import",
             str(self.server.database.directory), str(source)],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(connection.read_bytes(), before)
        self.assertEqual(self.store.read_events(), [original])

    def test_artifact_upload_address_and_download_integrity(self) -> None:
        data = b"artifact"
        path = f"blobs/{hashlib.sha256(data).hexdigest()}/model.bin"
        endpoint = f"/api/v2/datasets/{self.store.config['media_dataset_rid']}/files/"
        from urllib.parse import quote
        with self.assertRaises(BackendError):
            self.store.request(endpoint + quote(path, safe="") + "/upload?branchName=master",
                               "POST", b"wrong", "application/octet-stream")
        source = self.root / "artifact.bin"
        source.write_bytes(data)
        reference = self.store.upload_file(source)
        (self.server.database.artifacts / reference["sha256"]).write_bytes(b"corrupt")
        with patch("backprop.foundry.time.sleep"), self.assertRaises(BackendError):
            self.store.download_file(reference)

    def test_archive_cursor_and_control_recovery_keep_the_existing_query_contract(self) -> None:
        records = [event(f"page-{index}", step=index) for index in range(7)]
        self.store.publish(records)
        self.store._archive_page_size = 2
        self.assertEqual(len(self.store.query_archive(project="project")), 7)
        reader = self.store.control_reader(project="project", entity="local",
                                           run_id="run-1", instance_id="current")
        stop = {**event("archived-stop"), "type": "run_stop_request",
                "payload": {"action": "stop", "instance_id": "current"}}
        self.store.publish([stop])
        self.assertEqual(reader._archive(), [stop])

    def test_incremental_import_reuses_atomic_resolution_without_source_cache_changes(self) -> None:
        """
        Purpose: Verify incremental import reuses atomic resolution without source cache changes.
        Input: Isolated test data and the real backend interfaces.
        Output: Assertions for the documented storage contract.
        """
        settings = {"console": "off", "silent": True, "disable_git": True,
                    "x_disable_stats": True}
        base = backprop.Run(project="increments", entity="local", id="base",
                            dir=self.root / "base", settings=settings,
                            _sink=self.store, _deployment=self.server.connection)
        base_artifact = backprop.Artifact("weights", "model")
        with base_artifact.new_file("base.txt") as output:
            output.write("base")
        base.log_artifact(base_artifact)
        base.finish()
        offline = backprop.Run(project="increments", entity="local", id="delta",
                               mode="offline", dir=self.root / "offline", settings=settings)
        delta = backprop.Artifact("weights", "model", incremental=True)
        with delta.new_file("delta.txt") as output:
            output.write("delta")
        offline.log_artifact(delta)
        offline.finish()
        directory = Path(offline.sync_dir)
        before = {str(path.relative_to(directory)): path.read_bytes()
                  for path in directory.rglob("*") if path.is_file()}
        self.store.import_journal(directory)
        count = self.server.database.end_offset()
        self.store.import_journal(directory)
        self.assertEqual(self.server.database.end_offset(), count)
        self.assertEqual(before, {str(path.relative_to(directory)): path.read_bytes()
                                  for path in directory.rglob("*") if path.is_file()})
        restored = backprop.Api().artifact("local/increments/weights:latest")
        self.assertEqual(set(restored.manifest.entries), {"base.txt", "delta.txt"})

    def test_offline_branch_import_preserves_the_original_branch_files(self) -> None:
        """
        Purpose: Verify offline branch import preserves the original branch files.
        Input: Isolated test data and the real backend interfaces.
        Output: Assertions for the documented storage contract.
        """
        settings = {"console": "off", "silent": True, "disable_git": True,
                    "x_disable_stats": True}
        parent = backprop.Run(project="branches", entity="local", id="parent",
                              dir=self.root / "parents", settings=settings,
                              _sink=self.store, _deployment=self.server.connection)
        for index in range(3):
            parent.log({"loss": 3 - index}, step=index, commit=True)
        parent.finish()
        child = backprop.Run(project="branches", entity="local", id="child",
                             mode="offline", dir=self.root / "children",
                             settings={**settings, "fork_from": "parent?_step=1"})
        child.log({"loss": 0.25}, commit=True)
        child.finish()
        directory = Path(child.sync_dir)
        before = {str(path.relative_to(directory)): path.read_bytes()
                  for path in directory.rglob("*") if path.is_file()}
        self.store.import_journal(directory)
        count = self.server.database.end_offset()
        self.store.import_journal(directory)
        self.assertEqual(self.server.database.end_offset(), count)
        self.assertEqual(before, {str(path.relative_to(directory)): path.read_bytes()
                                  for path in directory.rglob("*") if path.is_file()})
        history = backprop.Api().run("local/branches/child").history(pandas=False)
        self.assertEqual([row["loss"] for row in history], [3, 2, 0.25])

    def test_new_installation_cannot_reuse_another_local_acknowledgement(self) -> None:
        journal = Journal(self.root / "same-journal", self.store)
        journal.append(event("installation-scoped"))
        journal.flush()
        with running_server(self.root / "another-database") as server:
            store = LocalStore(server.connection)
            replay = Journal(journal.directory, store)
            self.assertEqual(replay.ack, 0)
            replay.flush()
            self.assertEqual(server.database.end_offset(), 1)
            stale = {**server.connection, "local_id": self.store.config["local_id"]}
            with self.assertRaisesRegex(ValueError, "installation changed"):
                LocalStore(stale)

    def test_import_preserves_live_media_symlinks_and_source_state(self) -> None:
        """
        Purpose: Import authorized live media without changes to the source journal.
        Input: An offline journal with a symlink and a Foundry acknowledgement.
        Output: Verified local media bytes and an unchanged source after replay.
        """
        source = self.root / "linked.bin"
        source.write_bytes(b"before")
        run = backprop.Run(
            project="linked-import", entity="local", mode="offline",
            dir=self.root / "linked-runs",
            settings={"allow_media_symlink": True, "console": "off",
                      "silent": True, "disable_git": True, "x_disable_stats": True},
        )
        self.addCleanup(run.finish)
        run.log({"media": EncodedMedia(source)})
        run.finish()
        directory = Path(run.sync_dir)
        linked = next(path for path in directory.rglob("*") if path.is_symlink())
        self.assertEqual(linked.readlink(), source.resolve())
        (directory / "ack.json").write_text('{"count":1,"byte_offset":1}')
        source.write_bytes(b"before import")
        files = {path: path.read_bytes() for path in directory.rglob("*")
                 if path.is_file() and not path.is_symlink()}
        links = {path: path.readlink() for path in directory.rglob("*")
                 if path.is_symlink()}
        paths = set(directory.rglob("*"))

        sync(run.sync_dir, sink=self.store)
        offset = self.server.database.end_offset()
        sync(run.sync_dir, sink=self.store)

        self.assertEqual(self.server.database.end_offset(), offset)
        self.assertEqual({path: path.read_bytes() for path in files}, files)
        self.assertEqual({path: path.readlink() for path in links}, links)
        self.assertEqual(set(directory.rglob("*")), paths)
        history = self.store.read_events(project="linked-import", run_id=run.id)
        reference = next(row["payload"]["metrics"]["media"] for row in history
                         if row["type"] == "history")
        self.assertEqual(self.store.download_file(reference), b"before import")

    def test_identical_local_artifact_rounds_get_distinct_operations(self) -> None:
        """
        Purpose: Start a fresh artifact round after a local acknowledgement.
        Input: Two fresh artifacts with identical content on one writer.
        Output: Two distinct operations and two independently committed versions.
        """
        run = backprop.Run(
            project="identical-rounds", entity="local", group="workers",
            dir=self.root / "rounds", _sink=self.store,
            _deployment=self.server.connection,
            settings={"console": "off", "silent": True, "disable_git": True,
                      "x_disable_stats": True},
        )
        self.addCleanup(run.finish)
        artifacts = []
        for _ in range(2):
            artifact = backprop.Artifact("model", "model", metadata={"same": True})
            with artifact.new_file("weights.txt") as output:
                output.write("identical weights")
            run.upsert_artifact(artifact)
            run.finish_artifact(artifact)
            artifacts.append(artifact)
        run.finish()

        self.assertNotEqual(artifacts[0]._operation_id, artifacts[1]._operation_id)
        self.assertNotEqual(artifacts[0].id, artifacts[1].id)
        events = self.store.read_events(project="identical-rounds", run_id=run.id)
        for kind in ("artifact_part", "artifact"):
            records = [row for row in events if row["type"] == kind]
            self.assertEqual(len(records), 2)
            self.assertEqual(len({row["payload"]["operation_id"] for row in records}), 2)
        for artifact in artifacts:
            self.assertEqual(artifact.state, "COMMITTED")
            self.assertEqual(artifact.get_entry("weights.txt")._read_verified_bytes(),
                             b"identical weights")

    def test_import_keeps_live_media_after_restart_and_interrupted_responses(self) -> None:
        """
        Purpose: Retain imported media choices through edits, deletion, and restart.
        Input: An offline journal and successful or interrupted local imports.
        Output: Later records commit with the first media bytes and intact source state.
        """
        settings = {"allow_media_symlink": True, "console": "off", "silent": True,
                    "disable_git": True, "x_disable_stats": True}
        for fault in ("none", "claim", "publish"):
            with self.subTest(fault=fault):
                source = self.root / (fault + ".bin")
                source.write_bytes(b"first choice")
                run = backprop.Run(project="frozen-import", entity="local",
                                   mode="offline", dir=self.root / fault,
                                   settings=settings)
                self.addCleanup(run.finish)
                run.log({"media": EncodedMedia(source)})
                run.finish()
                directory = Path(run.sync_dir)
                (directory / "ack.json").write_text('{"count":1,"byte_offset":1}')
                originals = {path: path.read_bytes() for path in directory.rglob("*")
                             if path.is_file() and not path.is_symlink()}
                database = self.root / (fault + "-database")
                with running_server(database) as server:
                    store = LocalStore(server.connection)
                    if fault == "none":
                        store.import_journal(directory)
                    else:
                        name = "claim_artifact_resolution" if fault == "claim" else "publish"
                        original = getattr(store, name)

                        def lost(*args, **kwargs):
                            original(*args, **kwargs)
                            raise OSError("response lost after commit")

                        with patch.object(store, name, side_effect=lost):
                            with self.assertRaisesRegex((OSError, BackpropError), "response lost"):
                                store.import_journal(directory)
                self.assertEqual({path: path.read_bytes() for path in originals}, originals)
                source.write_bytes(b"changed after first attempt")
                with running_server(database) as server:
                    store = LocalStore(server.connection)
                    store.import_journal(directory)
                    offset = server.database.end_offset()
                    source.unlink()
                    extra = {**event("extra-" + fault), "project": "frozen-import",
                             "run_id": run.id}
                    Journal(directory).append(extra)
                    store.import_journal(directory)
                    self.assertEqual(server.database.end_offset(), offset + 1)
                    rows = store.read_events(project="frozen-import", run_id=run.id)
                    reference = next(row["payload"]["metrics"]["media"] for row in rows
                                     if row["type"] == "history" and "media" in row["payload"]["metrics"])
                    self.assertEqual(store.download_file(reference), b"first choice")
                    self.assertIn(extra, rows)
                for path, data in originals.items():
                    if path.name == "events.jsonl":
                        self.assertTrue(path.read_bytes().startswith(data))
                    else:
                        self.assertEqual(path.read_bytes(), data)

    def test_concurrent_importers_use_the_same_live_media_claim(self) -> None:
        """
        Purpose: Publish one media descriptor when concurrent snapshots contain different bytes.
        Input: Two imports with a source edit before the first storage claim.
        Output: Both imports succeed with the same durable winner and stream offsets.
        """
        source = self.root / "race.bin"
        source.write_bytes(b"first candidate")
        run = backprop.Run(
            project="media-race", entity="local", mode="offline", dir=self.root / "race",
            settings={"allow_media_symlink": True, "console": "off", "silent": True,
                      "disable_git": True, "x_disable_stats": True},
        )
        self.addCleanup(run.finish)
        run.log({"media": EncodedMedia(source)})
        run.finish()
        ready, release = threading.Event(), threading.Event()
        original = self.store.claim_artifact_resolution

        def delayed(key, candidate):
            if candidate.get("kind") == "local-journal-live-media":
                ready.set()
                if not release.wait(5):
                    raise TimeoutError("Concurrent import did not finish")
            return original(key, candidate)

        with ThreadPoolExecutor(max_workers=1) as workers, \
                patch.object(self.store, "claim_artifact_resolution", side_effect=delayed):
            first = workers.submit(self.store.import_journal, run.sync_dir)
            try:
                self.assertTrue(ready.wait(3))
                source.write_bytes(b"second candidate wins")
                other = LocalStore(self.server.connection)
                count = other.import_journal(run.sync_dir)
                offset = self.server.database.end_offset()
            finally:
                release.set()
            self.assertEqual(first.result(timeout=5), count)
        self.assertEqual(self.server.database.end_offset(), offset)
        rows = self.store.read_events(project="media-race", run_id=run.id)
        reference = next(row["payload"]["metrics"]["media"] for row in rows
                         if row["type"] == "history")
        self.assertEqual(self.store.download_file(reference), b"second candidate wins")

    def test_materialized_artifacts_replay_into_another_backend(self) -> None:
        """
        Purpose: Keep backend file references and resolution caches in separate scopes.
        Input: Incremental and distributed journals materialized on two backend types.
        Output: The new local installation has verified files and accepts later import.
        """
        settings = {'console': 'off', 'silent': True, 'disable_git': True,
                    'x_disable_stats': True}
        for kind in ('incremental', 'distributed'):
            for previous_backend in ('legacy', 'local'):
                with self.subTest(kind=kind, previous_backend=previous_backend):
                    name = kind + '-' + previous_backend
                    previous = backprop.MemorySink() if previous_backend == 'legacy' else self.store
                    run = backprop.Run(project=name, entity='local', id='cached',
                                       group='workers', mode='offline',
                                       dir=self.root / name, settings=settings)
                    artifact = backprop.Artifact('weights', 'model', incremental=kind == 'incremental')
                    with artifact.new_file('weights.txt') as output:
                        output.write(name)
                    if kind == 'distributed':
                        run.finish_artifact(artifact)
                    else:
                        run.log_artifact(artifact)
                    run.config['model'] = artifact
                    run.finish()
                    directory = Path(run.sync_dir)
                    Journal(directory, previous).flush()
                    original = {path: path.read_bytes() for path in directory.rglob('*') if path.is_file()}
                    with running_server(self.root / (name + '-destination')) as server:
                        store = LocalStore(server.connection)
                        replay = Journal(directory, store)
                        self.assertEqual(replay.ack, 0)
                        replay.flush()
                        api = backprop.Api._from_store(store, entity='local', project=name)
                        found = api.artifact('weights:latest')
                        self.assertEqual(found.get_entry('weights.txt')._read_verified_bytes(), name.encode())
                        count = server.database.end_offset()
                        store.import_journal(directory)
                        self.assertEqual(server.database.end_offset(), count)
                        self.assertEqual({path: path.read_bytes() for path in original}, original)
                        resumed = backprop.Run(project=name, entity='local', id='cached',
                                               group='workers', resume='must',
                                               dir=self.root / name, settings=settings,
                                               _sink=store, _deployment=server.connection)
                        try:
                            model = resumed.config['model']
                            self.assertEqual(model.state, 'COMMITTED')
                            self.assertEqual(model.get_entry('weights.txt')._read_verified_bytes(), name.encode())
                            extra = backprop.Artifact('weights', 'model', incremental=True)
                            with extra.new_file('extra.txt') as output:
                                output.write('extra')
                            resumed.log_artifact(extra)
                            self.assertEqual(set(extra.manifest.entries), {'weights.txt', 'extra.txt'})
                        finally:
                            resumed.finish()

    def test_sdk_metrics_history_resume_tables_artifacts_and_no_credentials(self) -> None:
        """
        Purpose: Verify sdk metrics history resume tables artifacts and no credentials.
        Input: Isolated test data and the real backend interfaces.
        Output: Assertions for the documented storage contract.
        """
        settings = {"console": "off", "disable_git": True,
                    "x_disable_stats": True, "flush_interval": 3600}
        with patch("backprop.login_prompt.prompt_token", side_effect=AssertionError("No login")):
            self.assertTrue(backprop.login())
            run = backprop.init(project="integration", entity="local", id="resume-test",
                                dir=self.root / "runs", settings=settings)
            run.log({"loss": 0.5, "table": backprop.Table(columns=["x"], data=[[1]])})
            model = self.root / "model.bin"
            model.write_bytes(b"weights")
            artifact = backprop.Artifact("model", type="model")
            artifact.add_file(str(model))
            run.log_artifact(artifact, aliases=["latest"])
            run.finish()
            api = backprop.Api()
            observed = api.run("local/integration/resume-test")
            self.assertEqual(observed.history(pandas=False)[0]["loss"], 0.5)
            restored = api.artifact("local/integration/model:latest")
            destination = restored.download(root=str(self.root / "download"))
            self.assertEqual((Path(destination) / "model.bin").read_bytes(), b"weights")
            resumed = backprop.init(project="integration", entity="local", id="resume-test",
                                    resume="must", dir=self.root / "resumed", settings=settings)
            self.assertTrue(resumed.resumed)
            resumed.log({"loss": 0.25})
            resumed.finish()
            api.flush()
            history = api.run("local/integration/resume-test").history(pandas=False)
            self.assertEqual([row["loss"] for row in history], [0.5, 0.25])

    def test_control_reader_and_cooperative_stop_target_current_instance(self) -> None:
        """
        Purpose: Verify control reader and cooperative stop target current instance.
        Input: Isolated test data and the real backend interfaces.
        Output: Assertions for the documented storage contract.
        """
        stopped = threading.Event()
        settings = {"console": "off", "disable_git": True, "x_disable_stats": True,
                    "stop_fn": stopped.set, "flush_interval": 0.1}
        with patch("backprop.run_stopping.POLL_INTERVAL", 0.02):
            run = backprop.init(project="controls", entity="local", id="controlled",
                                dir=self.root / "control", settings=settings)
        self.addCleanup(run.finish)
        run.flush()
        events = self.store.read_events(project="controls", run_id=run.id, entity="local")
        start = next(row for row in events if row["type"] == "run_start")
        instance = start["payload"]["control"]["instance_id"]
        stop = {**event("stop-request"), "run_id": run.id, "project": "controls",
                "type": "run_stop_request", "payload": {"action": "stop", "instance_id": instance}}
        self.store.publish([stop, stop])
        self.assertTrue(stopped.wait(5))
        run.flush()
        acknowledgements = [row for row in self.store.read_events(project="controls")
                            if row["type"] == "run_stop_ack"]
        self.assertEqual(len(acknowledgements), 1)

    def test_local_mode_does_not_send_foundry_tokens(self) -> None:
        original = self.store.opener.open
        authorization = []

        def capture(req, **kwargs):
            authorization.append(req.get_header("Authorization"))
            return original(req, **kwargs)

        with patch.object(self.store.opener, "open", side_effect=capture):
            self.store.publish([event("no-token")])
        self.assertEqual(authorization, [None])

    def test_media_ranges_preserve_verified_artifact_bytes(self) -> None:
        source = self.root / "media.bin"
        source.write_bytes(b"0123456789")
        reference = self.store.upload_file(source)
        url = self.store.file_url(reference)
        for byte_range, expected in (("bytes=2-5", b"2345"), ("bytes=-3", b"789")):
            req = request.Request(url, headers={"Range": byte_range})
            with request.urlopen(req) as response:
                self.assertEqual(response.status, 206)
                self.assertEqual(response.read(), expected)
        from urllib.error import HTTPError
        req = request.Request(url, headers={"Range": "bytes=9-2"})
        with self.assertRaises(HTTPError) as raised:
            request.urlopen(req)
        self.assertEqual(raised.exception.code, 416)
        raised.exception.close()

    def test_sql_subset_rejects_arbitrary_sql_and_preserves_encoded_filters(self) -> None:
        original = event("scope")
        original["project"] = "quoted ' project\n雪"
        self.store.publish([original])
        self.assertEqual(self.store.read_events(project=original["project"]), [original])
        with self.assertRaises(BackendError):
            self.store._query_csv("SELECT * FROM events; DELETE FROM events")
        self.assertEqual(self.server.database.end_offset(), 1)

    def test_origin_guard_refuses_foreign_browser_writes(self) -> None:
        req = request.Request(self.server.origin + "/config.json",
                              headers={"Origin": "https://foreign.example"})
        from urllib.error import HTTPError
        with self.assertRaises(HTTPError) as raised:
            request.urlopen(req)
        self.assertEqual(raised.exception.code, 403)
        raised.exception.close()

    def test_foundry_https_and_token_requirements_remain(self) -> None:
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            FoundryStore(self.server.connection, token="token")
        with self.assertRaisesRegex(ValueError, "loopback"):
            normalize_local_config({"backend": "local", "host": "http://example.com"})
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            native_store({**self.server.connection, "backend": "foundry"})
        with self.assertRaisesRegex(ValueError, "loopback"):
            create_server(self.root / "unsafe", host="0.0.0.0", port=0)


if __name__ == "__main__":
    unittest.main()
