"""Verify the stop-control permission change preserves existing app restrictions.

Architecture: a recorded-shape API fixture -> plan/apply -> exact update body
and independent readback. No network or credentials are used by these tests.
Main callers: unittest discovery and deployment verification.
"""
import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'deploy'))
from configure_controls import configure, planned_operations

ORGANIZATION = 'ri.multipass..organization.customer'


class ApplicationApi:
    def __init__(self):
        self.application = {'rid': 'application', 'name': 'Backprop',
            'scopes': {'authScope': {'platform': {'projects': {'project': [{'type': 'manual', 'manual': {}}]}}}},
            'clientAllowedOperationsV2': {'version': 'V1', 'operations': ['STREAMS_READ', 'DATASETS_READ']},
            'clientAllowedMarkingIdsV2': ['marking'],
            'clientSpecification': {'type': 'public', 'public': {'redirectUrls': ['https://example.invalid/']}}}
        self.installation = {'resources': ['project'], 'operations': [], 'markingIds': ['marking'],
                             'grantTypes': ['AUTHORIZATION_CODE', 'REFRESH_TOKEN'], 'requireConsent': True}
        self.writes = []
        self.read_paths = []
        self.mutate_other_field = False

    def request(self, path, method='GET', body=None):
        if method != 'GET':
            assert path.endswith('/scoping') and method == 'PUT'
            self.writes.append(copy.deepcopy(body))
            self.application['clientAllowedOperationsV2']['operations'] = body['clientAllowedOperations']
            if self.mutate_other_field:
                self.application['clientAllowedMarkingIdsV2'] = []
            return None
        self.read_paths.append(path)
        if '/client-installations/' in path:
            return {'installation': copy.deepcopy(self.installation)}
        return {'application': copy.deepcopy(self.application)}


class ControlConfigurationTests(unittest.TestCase):
    state = {'application_rid': 'application', 'client_id': 'client', 'project_rid': 'project'}

    def test_plan_is_read_only_and_apply_changes_only_one_operation(self):
        api = ApplicationApi()
        before = copy.deepcopy(api.application)
        self.assertTrue(configure(api, self.state, organization_rid=ORGANIZATION)['change_needed'])
        self.assertEqual(api.writes, [])
        self.assertEqual(api.application, before)
        result = configure(api, self.state, organization_rid=ORGANIZATION, apply=True)
        self.assertTrue(result['verified'])
        self.assertEqual(api.writes, [{'clientAllowedOperations': ['DATASETS_READ', 'STREAMS_READ', 'STREAMS_WRITE']}])
        self.assertFalse(configure(api, self.state, organization_rid=ORGANIZATION, apply=True)['applied'])
        self.assertEqual(len(api.writes), 1)
        installation_reads = [path for path in api.read_paths if '/client-installations/' in path]
        self.assertTrue(installation_reads)
        self.assertTrue(all('organizationRid=' + ORGANIZATION in path for path in installation_reads))

    def test_unscoped_or_wrong_project_app_is_rejected_before_mutation(self):
        for change in ('project', 'operations', 'version'):
            api = ApplicationApi()
            if change == 'project': api.application['scopes'] = {}
            elif change == 'operations': del api.application['clientAllowedOperationsV2']['operations']
            else: api.application['clientAllowedOperationsV2']['version'] = 'unknown'
            with self.subTest(change=change), self.assertRaises(ValueError):
                configure(api, self.state, organization_rid=ORGANIZATION, apply=True)
            self.assertEqual(api.writes, [])

    def test_explicit_installation_restrictions_are_preserved(self):
        api = ApplicationApi(); api.installation['operations'] = ['api:use-streams-read']
        with self.assertRaisesRegex(ValueError, 'installation explicitly restricts'):
            configure(api, self.state, organization_rid=ORGANIZATION, apply=True)
        self.assertEqual(api.writes, [])
        api.installation['operations'].append('api:use-streams-write')
        before = copy.deepcopy(api.installation)
        self.assertTrue(configure(api, self.state, organization_rid=ORGANIZATION, apply=True)['verified'])
        self.assertEqual(api.installation, before)

    def test_verification_detects_an_unrelated_restriction_change(self):
        api = ApplicationApi(); api.mutate_other_field = True
        with self.assertRaisesRegex(RuntimeError, 'single planned operation'):
            configure(api, self.state, organization_rid=ORGANIZATION, apply=True)


if __name__ == '__main__':
    unittest.main()
