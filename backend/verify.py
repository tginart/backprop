"""Verify the deployed Foundry storage contract without a browser.

Architecture: create labeled probe events -> publish/read live -> upload/read a
small file -> wait for automatic archive -> confirm SQL and replay agree.
Main callers: deployment operators. Writes are confined to Backprop resources.
Credentials come from the existing deployment helper and stay only in memory.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import time
from urllib.parse import quote
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "deploy"), str(ROOT / "sdk")]
from foundry_api import FoundryApi
from local_config import load_deployment_config, read_resource_state
from backprop.foundry import FoundryStore


def verify(wait_seconds, *, config_path=None, state_path=None,
           entity="backprop", project="backprop-system"):
    """Require byte-exact live, media, and archived readback of new probe data.

    Main callers: main. Validate the selected installation before obtaining its
    credential; every generated event stays in the requested probe namespace.
    """
    config = load_deployment_config(config_path)
    state = read_resource_state(config, state_path)
    api = FoundryApi(config)
    store = FoundryStore(state, api.token)
    run_id = "backend-probe-" + uuid.uuid4().hex[:8]
    stamp = datetime.now(timezone.utc).isoformat()
    common = {"schema_version": 1, "run_id": run_id, "project": project,
              "entity": entity, "timestamp": stamp}
    events = [
        {**common, "event_id": str(uuid.uuid4()), "type": "run_start",
         "payload": {"name": "Native storage verification", "tags": ["system", "verification"],
                     "config": {"purpose": "Foundry integration test"}}},
        {**common, "event_id": str(uuid.uuid4()), "type": "history",
         "payload": {"step": 0, "metrics": {"verification/value": 0.125}}},
        {**common, "event_id": str(uuid.uuid4()), "type": "run_finish",
         "payload": {"state": "finished", "exit_code": 0}},
    ]
    start = time.monotonic()
    store.publish(events)
    expected = {event["event_id"]: event for event in events}
    live = {row["event_id"]: store.record_to_event(row) for row in store.read_live_records()
            if row["run_id"] == run_id}
    if live != expected:
        raise AssertionError("Live stream readback differs from published events")
    live_seconds = time.monotonic() - start
    print(f"Live event readback passed in {live_seconds:.2f}s", flush=True)
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "verification.txt"
        content = b"Backprop exact file roundtrip\n"
        path.write_bytes(content)
        reference = store.upload_file(path)
        downloaded = store.request(f"/api/v2/datasets/{reference['dataset_rid']}/files/"
                                   + quote(reference["path"], safe="") + "/content")
        if downloaded != content:
            raise AssertionError("Media upload/download changed bytes")
    print("Media byte roundtrip passed", flush=True)
    deadline = start + wait_seconds
    while True:
        archived = {row["event_id"]: store.record_to_event(row) for row in
                    store.query_archive(entity=entity, project=project, run_id=run_id)}
        if archived == expected:
            break
        if time.monotonic() >= deadline:
            raise TimeoutError(f"Archive did not contain all probe events within {wait_seconds}s")
        time.sleep(10)
    archive_seconds = time.monotonic() - start
    replay = {event["event_id"]: event for event in
              store.read_events(project=project, run_id=run_id, entity=entity)}
    if replay != expected:
        raise AssertionError("Merged replay lost or changed events")
    return {"verified_at": datetime.now(timezone.utc).isoformat(), "run_id": run_id,
            "stream_publish_read": True, "live_readback_seconds": round(live_seconds, 2),
            "archive_query": True, "archive_readback_seconds": round(archive_seconds, 2),
            "archived_row_count": len(archived), "media_roundtrip": True,
            "media_sha256": hashlib.sha256(content).hexdigest(), "merged_replay": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, help="Operator config; defaults to BACKPROP_DEPLOY_CONFIG")
    parser.add_argument("--state", type=Path, help="Override the configured resource-state path")
    parser.add_argument("--entity", default="backprop")
    parser.add_argument("--project", default="backprop-system")
    parser.add_argument("--archive-timeout", type=int, default=420)
    args = parser.parse_args()
    result = verify(args.archive_timeout, config_path=args.config, state_path=args.state,
                    entity=args.entity, project=args.project)
    (ROOT / "backend/verification.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
