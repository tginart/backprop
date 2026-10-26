"""
Purpose: Measure native Firestore ingestion work as history and writer count grow.
Input: An explicit disposable Firestore database, private bucket, and report path.
Output: Latency, document-operation counts, request bytes, and GCS call counts.
Note: This test appends to a unique installation prefix. Delete the test database afterward.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import statistics
import threading
import time
from unittest.mock import patch
import uuid

from google.cloud import firestore, storage
from google.cloud.firestore_v1 import types
from google.auth.credentials import AnonymousCredentials
from google.oauth2.credentials import Credentials

from backprop_backend.gcp.database import FirestoreDatabase


def benchmark(args: argparse.Namespace) -> dict:
    """
    Purpose: Verify that metrics ingestion does not transfer existing history.
    Input: Isolated cloud resource selectors and bounded workload sizes.
    Output: A report with independent history and concurrency measurements.
    """
    credentials = None
    emulator = bool(os.environ.get("FIRESTORE_EMULATOR_HOST"))
    if emulator:
        credentials = AnonymousCredentials()
    elif os.environ.get("BACKPROP_GCP_ACCESS_TOKEN"):
        credentials = Credentials(os.environ["BACKPROP_GCP_ACCESS_TOKEN"])
    client = firestore.Client(project=args.project, database=args.database, credentials=credentials)
    bucket = storage.Client(project=args.project, credentials=credentials).bucket(args.bucket)
    prefix = "benchmark-" + uuid.uuid4().hex
    database = FirestoreDatabase(client, bucket, prefix=prefix)
    report = {"prefix": prefix, "emulator": emulator, "history": [], "concurrency": [],
              "note": "Counts describe client requests, not an exported billing audit."}
    count = 0

    def records(size: int) -> list[dict]:
        nonlocal count
        start, count = count, count + size
        timestamp = datetime.now(timezone.utc).isoformat()
        return [{"schema_version": 1, "event_id": f"event-{index:012d}",
                 "run_id": "benchmark", "project": "benchmark", "entity": "verification",
                 "event_type": "history", "timestamp": timestamp,
                 "payload": json.dumps({"step": index, "metrics": {"loss": 1.0}})}
                for index in range(start, start + size)]

    def measure(batches: list[list[dict]], *, workers: int) -> dict:
        """
        Purpose: Observe actual native requests without backend instrumentation endpoints.
        Input: Prepared batches and a bounded writer count.
        Output: Wall time, latency quantiles, document counts, and request bytes.
        """
        api, lock = client._firestore_api, threading.Lock()
        counters = {"document_reads_requested": 0, "document_writes_requested": 0,
                    "request_bytes": 0, "gcs_calls": 0}

        def observe(name, message_type, count_field, documents_field):
            original = getattr(api, name)

            def call(*positional, **options):
                message = message_type(options["request"])
                with lock:
                    counters[count_field] += len(getattr(message, documents_field))
                    counters["request_bytes"] += message._pb.ByteSize()
                return original(*positional, **options)

            return call

        def publish(batch):
            start = time.monotonic()
            database.publish(batch)
            return time.monotonic() - start

        start = time.monotonic()
        with ExitStack() as stack:
            stack.enter_context(patch.object(api, "batch_get_documents", observe(
                "batch_get_documents", types.BatchGetDocumentsRequest,
                "document_reads_requested", "documents")))
            stack.enter_context(patch.object(api, "commit", observe(
                "commit", types.CommitRequest, "document_writes_requested", "writes")))
            gcs = stack.enter_context(patch.object(bucket, "blob", wraps=bucket.blob))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                latencies = list(pool.map(publish, batches))
            counters["gcs_calls"] = gcs.call_count
        ordered = sorted(latencies)
        expected = sum(len(batch) + 1 for batch in batches)
        if counters["gcs_calls"] or counters["document_writes_requested"] < expected:
            raise AssertionError("Ingestion performed GCS I/O or omitted event/cursor writes")
        return {"batches": len(batches), "records": sum(map(len, batches)),
                "workers": workers, "seconds": round(time.monotonic() - start, 4),
                "p50_ms": round(statistics.median(ordered) * 1000, 3),
                "p95_ms": round(ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))] * 1000, 3),
                "expected_document_operations_per_batch": args.batch_size + 1,
                **counters}

    try:
        for target in args.histories:
            seed_start, seed_count = time.monotonic(), 0
            while count < target:
                batch = records(min(500, target - count))
                database.publish(batch)
                seed_count += len(batch)
                time.sleep(max(0, seed_count / args.seed_rate - (time.monotonic() - seed_start)))
            before = count
            measured = measure([records(args.batch_size) for _ in range(args.samples)], workers=1)
            measured["history_before"] = before
            expected = args.samples * (args.batch_size + 1)
            if (measured["document_reads_requested"] != expected
                    or measured["document_writes_requested"] != expected):
                raise AssertionError("Uncontended ingestion work changed with stored history")
            report["history"].append(measured)
            print(json.dumps({"history": measured}), flush=True)
        for workers in (1, 4, 8):
            measured = measure([records(args.batch_size) for _ in range(args.samples)], workers=workers)
            report["concurrency"].append(measured)
            print(json.dumps({"concurrency": measured}), flush=True)
        if database.end_offset() != count:
            raise AssertionError("Concurrent benchmark lost or duplicated records")
        report.update(status="passed", committed_records=count)
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + "\n")
        return report
    finally:
        client.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True, help="Explicit disposable Google project")
    parser.add_argument("--database", required=True, help="Explicit disposable Firestore database")
    parser.add_argument("--bucket", required=True, help="Test installation's private bucket")
    parser.add_argument("--report", type=Path, required=True, help="Private JSON report path")
    parser.add_argument("--histories", type=int, nargs="+", default=[0, 5000, 20000], help="Increasing stored-record targets")
    parser.add_argument("--batch-size", type=int, default=50, help="Events per measured transaction")
    parser.add_argument("--samples", type=int, default=20, help="Measured transactions per stage")
    parser.add_argument("--seed-rate", type=int, default=250, help="Maximum historical seed events per second")
    args = parser.parse_args()
    if (not 1 <= args.batch_size <= 500 or args.samples < 2 or args.seed_rate < 1
            or args.histories != sorted(set(args.histories)) or args.histories[0] < 0):
        parser.error("Use valid batch sizes, positive rates, and increasing nonnegative history sizes")
    benchmark(args)


if __name__ == "__main__":
    main()
