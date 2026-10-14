"""Enable run-stop publishing within the existing restricted Backprop project.

Architecture: read existing application/installation -> plan one stream-write
operation -> apply the granular scoping update -> reread and verify every other
application restriction. The browser still asks for write consent only when a
user enables stop controls. No project, dataset, client or grant type is created.
Main callers: operator CLI before deploying the run-control frontend.
Why this exists: logging uses the operator token, while browser stop requests
need a separately permitted operation on the existing public OAuth application.
"""
from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
from urllib.parse import urlencode

from foundry_api import FoundryApi
from local_config import load_deployment_config, read_deployment_state, read_resource_state, required_value


OPERATION = 'STREAMS_WRITE'
SCOPE = 'api:use-streams-write'


def planned_operations(application, project_rid):
    """Add one operation without weakening project or client restrictions.

    Main callers: configure and focused deployment tests. The source-audited
    Palantir scoping API updates only supplied fields; project grants, markings,
    redirect URLs, client specifications and existing operations stay intact.
    """
    projects = application.get('scopes', {}).get('authScope', {}).get('platform', {}).get('projects', {})
    if project_rid not in projects:
        raise ValueError('Existing application must already include the Backprop project')
    restrictions = application.get('clientAllowedOperationsV2', {})
    if restrictions.get('version') != 'V1' or not isinstance(restrictions.get('operations'), list):
        raise ValueError('Expected the existing explicitly restricted V1 application operations')
    operations = restrictions['operations']
    if any(not isinstance(item, str) for item in operations):
        raise ValueError('Application operations must be named strings')
    return sorted(set(operations) | {OPERATION})


def configure(api, state, *, organization_rid, apply=False):
    """Plan or apply the minimal operation change and verify authoritative state.

    Main callers: main. An installation with an empty operation list adds no
    restriction, matching Multipass enablement semantics. If an explicit list
    blocks the new operation, stop before changing the application: deployment
    must first prepare that separate installation change for review.
    """
    required_value(organization_rid, 'organization_rid')
    if 'organization_rid' in state and state['organization_rid'] != organization_rid:
        raise ValueError('Deployment state belongs to a different organization')
    base = '/third-party-application-service/api/applications/' + state['application_rid']
    before = api.request(base)['application']
    operations = planned_operations(before, state['project_rid'])
    installation_path = '/multipass/api/client-installations/' + state['client_id'] + '?' + urlencode({'organizationRid': organization_rid})
    installation = api.request(installation_path).get('installation')
    if not isinstance(installation, dict):
        raise ValueError('Existing OAuth installation is required')
    installed_operations = installation.get('operations')
    if installed_operations and SCOPE not in installed_operations:
        raise ValueError('Existing installation explicitly restricts stream writing; no changes applied')
    current = sorted(before['clientAllowedOperationsV2']['operations'])
    result = {'application_rid': state['application_rid'], 'operation': OPERATION,
              'scope': SCOPE, 'change_needed': current != operations, 'applied': False}
    if not apply:
        return result
    if current != operations:
        api.request(base + '/scoping', 'PUT', {'clientAllowedOperations': operations})
    after = api.request(base)['application']
    expected = copy.deepcopy(before)
    expected['clientAllowedOperationsV2']['operations'] = operations
    actual = copy.deepcopy(after)
    actual['clientAllowedOperationsV2']['operations'] = sorted(actual['clientAllowedOperationsV2']['operations'])
    if actual != expected:
        raise RuntimeError('Application verification differs from the single planned operation change')
    if api.request(installation_path).get('installation') != installation:
        raise RuntimeError('OAuth installation changed during verification')
    result.update(applied=current != operations, verified=True)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true', help='Apply and verify the one-operation change')
    parser.add_argument('--config', type=Path, help='Local deployment configuration')
    args = parser.parse_args()
    config = load_deployment_config(args.config)
    state = read_deployment_state(config, read_resource_state(config))
    organization_rid = state.get('organization_rid') or (config.bootstrap or {}).get('organization_rid')
    required_value(organization_rid, 'organization_rid')
    print(json.dumps(configure(FoundryApi(config), state, organization_rid=organization_rid, apply=args.apply), indent=2))


if __name__ == '__main__':
    main()
