"""Provision Backprop's project and read-only browser application.

Architecture: operator token -> dedicated project -> restricted public OAuth
client -> saved resource identifiers. Backend provisioning consumes project_rid.
No credentials are persisted or included in browser configuration.
Main callers: operator CLI during initial deployment.
"""
import argparse
import json
from pathlib import Path
from urllib.parse import urlencode
from uuid import UUID
from foundry_api import FoundryApi
from local_config import (load_deployment_config, read_optional_state, required_value,
                          save_state, validate_matching_states)

def organization_marking_id(api, organization_rid):
    """Resolve an organization's UUID marking separately from its resource RID.

    Main callers: main. OAuth marking restrictions accept marking UUIDs, while
    project organization fields accept organization RIDs. Mixing these identifiers
    can let application creation succeed but make token enrichment fail later.
    """
    organization = api.request("/api/v2/admin/organizations/" + organization_rid)
    marking_id = organization["markingId"]
    UUID(marking_id)
    return marking_id


def bootstrap(config, api=None):
    """Create only missing resources, stopping explicitly on any failed API call.

    Main callers: command-line entry point and isolated operator tests. Validate
    local installation bindings before retrieving credentials or making a call.
    """
    settings = config.bootstrap
    if settings is None:
        raise ValueError("Configure bootstrap.space_rid, organization_rid and owner_user_id, or use manual Foundry setup")
    space = required_value(settings.get("space_rid"), "bootstrap.space_rid")
    organization = required_value(settings.get("organization_rid"), "bootstrap.organization_rid")
    owner = required_value(settings.get("owner_user_id"), "bootstrap.owner_user_id")
    state = read_optional_state(config.deployment_state_path, config.host, "Deployment state")
    resources = read_optional_state(config.resource_state_path, config.host, "Resource state")
    validate_matching_states(state, resources)
    if resources.get("project_rid") is not None and state.get("project_rid") != resources["project_rid"]:
        raise ValueError("Record the existing resource project_rid in deployment state before bootstrap")
    if "organization_rid" in state and state["organization_rid"] != organization:
        raise ValueError("Deployment state belongs to a different organization")
    state.update(host=config.host, organization_rid=organization)
    api = FoundryApi(config) if api is None else api
    if api.host != config.host:
        raise ValueError("Operator API belongs to a different Foundry host")
    marking_id = organization_marking_id(api, organization)
    if "project_rid" not in state:
        project = api.request("/api/v2/filesystem/projects/create", "POST", {
            "displayName": "Backprop", "description": "Experiment logging and visualization.",
            "spaceRid": space, "organizationRids": [organization],
            "roleGrants": {"compass:manage": [{"principalId": owner, "principalType": "USER"}]},
            "defaultRoles": [],
        })
        state.update(project_rid=project["rid"], project_path=project["path"])
        save_state(config.deployment_state_path, state)
        print("Created project:", state["project_rid"], flush=True)
    if "application_rid" not in state:
        result = api.request("/third-party-application-service/api/applications/v5", "POST", {
            "name": "Backprop", "description": "Experiment logging and visualization on Foundry.",
            "organizationRid": organization, "parentFolderRid": state["project_rid"],
            "clientSpecification": {"type": "public", "public": {
                "authorizationCodeGrant": {"enabled": True, "redirectUrls": []},
                "refreshTokenGrant": {"enabled": True},
            }},
            "authScope": {"platform": {"projects": {
                state["project_rid"]: [{"type": "manual", "manual": {}}]
            }}},
            "clientAllowedOrganizations": [], "cbacMarkingIds": [],
            "clientAllowedOperations": ["STREAMS_READ", "DATASETS_READ", "SQL_QUERIES_EXECUTE", "SQL_QUERIES_READ"],
            "clientAllowedOperationsVersion": "V1",
            "clientAllowedMarkingIdsV2": [marking_id],
        })
        state["application_rid"] = result["application"]["rid"]
        save_state(config.deployment_state_path, state)
        print("Created application:", state["application_rid"], flush=True)
    print(json.dumps(state))
    if "client_id" not in state:
        client = api.request("/third-party-application-service/api/application-clients/" + state["application_rid"])
        if client["type"] == "notRequested":
            client = api.request("/third-party-application-service/api/application-clients/v2/" + state["application_rid"], "PUT")
        state["client_id"] = client["created"]["clientId"]
        save_state(config.deployment_state_path, state)
    installation_path = "/multipass/api/client-installations/" + state["client_id"] + "?" + urlencode({"organizationRid": organization})
    installation = api.request(installation_path)
    if not installation.get("installation"):
        api.request(installation_path, "PUT", {
            "resources": [state["project_rid"]],
            "operations": ["api:use-streams-read", "api:use-datasets-read", "api:use-sql-queries-execute", "api:use-sql-queries-read"],
            "markingIds": [marking_id], "grantTypes": ["AUTHORIZATION_CODE", "REFRESH_TOKEN"],
            "requireConsent": True,
        })
        print("Enabled read-only client for the Backprop project", flush=True)
    save_state(config.deployment_state_path, state)
    return state


def main():
    """Load explicit destination settings before the optional provisioning flow.

    Main callers: python deploy/bootstrap.py. This remains an experimental
    internal-API convenience; manual Developer Console setup is also supported.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, help="Local deployment configuration")
    args = parser.parse_args()
    bootstrap(load_deployment_config(args.config))


if __name__ == "__main__":
    main()
