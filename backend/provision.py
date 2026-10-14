"""Provision Backprop's two native Foundry storage resources.

Architecture: existing project -> fixed event stream + media dataset -> state file.
The stream stores append-only logging events. The media dataset stores immutable
content-addressed blobs. No web server, worker, database, or ontology is deployed.
Main callers: deployment operator. State is saved after each successful creation.
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "deploy"))
from foundry_api import FoundryApi
from local_config import load_deployment_config, read_optional_state, required_value, save_state

FIELDS = [
    ("schema_version", "integer"), ("event_id", "string"),
    ("run_id", "string"), ("project", "string"), ("entity", "string"),
    ("event_type", "string"), ("timestamp", "string"), ("payload", "string"),
]
STREAM_SCHEMA = {
    "fields": [
        {"name": name, "schema": {"nullable": False, "dataType": {"type": kind}}}
        for name, kind in FIELDS
    ],
    "keyFieldNames": ["run_id"],
}


def provision(api, project_rid, state_path):
    """Create missing resources, recording each RID before the next mutation.

    Main callers: main. Why this exists: re-running deployment must not create
    duplicate streams, and a media failure must preserve the successful stream.
    """
    required_value(project_rid, "project_rid")
    state = read_optional_state(state_path, api.host, "Resource state")
    if state.get("project_rid", project_rid) != project_rid:
        raise ValueError("State belongs to a different Foundry project")
    if "branch" in state and state["branch"] != "master":
        raise ValueError("Existing stream branch must be master for provisioning")
    state.update(host=api.host,
                 project_rid=project_rid, branch="master")
    def save():
        save_state(state_path, state)
    if "stream_dataset_rid" not in state:
        dataset = api.request("/api/v2/streams/datasets/create?preview=true", "POST", {
            "name": "Backprop Events", "parentFolderRid": project_rid,
            "schema": STREAM_SCHEMA, "branchName": "master",
            "partitionsCount": 1, "streamType": "LOW_LATENCY", "compressed": False,
        })
        state["stream_dataset_rid"] = dataset["rid"]
        save()
    if "media_dataset_rid" not in state:
        dataset = api.request("/api/v2/datasets", "POST", {
            "name": "Backprop Media", "parentFolderRid": project_rid,
        })
        state["media_dataset_rid"] = dataset["rid"]
        save()
    stream = api.request(f"/api/v2/streams/datasets/{state['stream_dataset_rid']}/streams/master")
    state["stream_view_rid"] = stream["viewRid"]
    save()
    return state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-rid", required=True)
    parser.add_argument("--state", type=Path, help="Override the local resource-state output")
    parser.add_argument("--config", type=Path, help="Local deployment configuration")
    args = parser.parse_args()
    config = load_deployment_config(args.config)
    required_value(args.project_rid, "project_rid")
    state_path = args.state or config.resource_state_path
    prior = read_optional_state(state_path, config.host, "Resource state")
    deployment = read_optional_state(config.deployment_state_path, config.host, "Deployment state")
    if any(state.get("project_rid", args.project_rid) != args.project_rid
           for state in (prior, deployment)):
        raise ValueError("State belongs to a different Foundry project")
    if "branch" in prior and prior["branch"] != "master":
        raise ValueError("Existing stream branch must be master for provisioning")
    print(json.dumps(provision(FoundryApi(config), args.project_rid, state_path), indent=2))


if __name__ == "__main__":
    main()
