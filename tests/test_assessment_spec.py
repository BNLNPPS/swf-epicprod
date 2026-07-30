import unittest

from swf_epicprod.assessment import reporting, spec


def artifact_with(issue, diagnosis=None):
    investigation = []
    if diagnosis is not None:
        investigation.append({
            'claim': 'Representative job establishes the failure phase.',
            'source': 'SWF Testbed panda_study_job',
            'request': {'pandaid': 1669921},
            'result': {'epicprod_diagnosis': diagnosis},
        })
    return {
        'schema_version': spec.SCHEMA_VERSION,
        'verdict': 'alarm',
        'axes': {
            axis: {'status': 'alarm' if axis == 'failures' else 'ok',
                   'note': 'Evidence checked.'}
            for axis in spec.AXES
        },
        'assessment': ['Output registration failed after valid payloads.'],
        'activity_interpretation': [],
        'software_findings': [],
        'top_issues': [issue],
        'dismissed': [],
        'outlook': [],
        'narration': 'Output registration requires operator attention.',
        'cites': {
            'narrative': 'campaign_26.07',
            'narrative_version': 1,
            'evidence_computed_at': '2026-07-30T10:00:00+00:00',
            'bundle_id': 'bundle-id',
        },
        'generation': {
            'consulted': [{
                'source': 'SWF Testbed',
                'contribution': 'PanDA error and job evidence.',
            }],
            'investigation': investigation,
            'problems': [],
            'unavailable': [],
        },
    }


def issue_with(attribution):
    return {
        'title': 'ASGC output registration failures',
        'severity': 'alarm',
        'evidence': ['PandaID 1669921'],
        'action': 'Restore remote checksum service.',
        'owner': 'ASGC storage operations',
        'attribution': attribution,
    }


class AssessmentAttributionTests(unittest.TestCase):
    def test_confirmed_attribution_must_match_preserved_job_diagnosis(self):
        attribution = {
            'phase': 'output_registration',
            'layer': 'storage',
            'entity': 'ASGC-XRD',
            'confidence': 'confirmed',
        }
        artifact = artifact_with(issue_with(attribution), {
            'phase': 'output_registration',
            'cause_layer': 'storage',
            'cause_entity': 'ASGC-XRD',
            'cause_confidence': 'confirmed',
            'endpoint': 'hpceph-xrootd.twgrid.org',
        })

        self.assertEqual(spec.validate_artifact(artifact), [])

    def test_causal_attribution_without_structured_evidence_is_rejected(self):
        artifact = artifact_with(issue_with({
            'phase': 'output_registration',
            'layer': 'storage',
            'entity': 'ASGC-XRD',
            'confidence': 'confirmed',
        }))

        self.assertIn(
            'top_issues[0].attribution does not match structured '
            'investigation evidence',
            spec.validate_artifact(artifact),
        )

    def test_unresolved_attribution_is_explicit_and_valid(self):
        artifact = artifact_with(issue_with({
            'phase': 'unresolved',
            'layer': 'unknown',
            'entity': '',
            'confidence': 'unresolved',
        }))

        self.assertEqual(spec.validate_artifact(artifact), [])
        rendered = reporting._issues(artifact['top_issues'])
        self.assertIn('| Attribution |', rendered)
        self.assertIn('| Unresolved |', rendered)


if __name__ == '__main__':
    unittest.main()
