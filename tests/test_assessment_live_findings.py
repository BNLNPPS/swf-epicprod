import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from swf_epicprod.assessment.bundle import _Manifest, _live_findings, assemble
from swf_epicprod.assessment import reporting


class LiveFindingsTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 28, 12, tzinfo=timezone.utc)

    def post(self, key, text, hours=1, **fields):
        return dict(dict(id=key, message=text, user_id='operator', root_id='',
                         create_at=int((self.now - timedelta(hours=hours)).timestamp()*1000)),
                    **fields)

    @patch.dict(os.environ, {'MATTERMOST_TOKEN': 'test-token',
                             'MATTERMOST_URL': 'chat.example', 'MATTERMOST_TEAM': 'main'}, clear=True)
    @patch('swf_epicprod.assessment.bundle._get')
    def test_complete_window_retains_full_findings_and_excludes_ai_reports(self, get):
        finding = self.post('finding', 'BNL-XRD certificate renewed; verified live. Evidence: URL')
        reply = self.post('reply', 'Resolved for the cited tasks.', root_id='finding')
        report = self.post('assessment', '### [Daily report](URL)\n**Daily AI assessment published**\nGenerated narration')
        older = self.post('old', 'outside the window', hours=30)
        future = self.post('future', 'after bundle time', hours=-1)
        get.side_effect = [
            {'id': 'team'}, {'id': 'channel'},
            {'order': ['future', 'reply', 'assessment'],
             'posts': {p['id']: p for p in (future, reply, report)}},
            {'order': ['reply', 'finding', 'old'],
             'posts': {p['id']: p for p in (reply, finding, older)}},
        ]
        manifest = _Manifest()
        evidence = _live_findings(self.now, 1, manifest)
        self.assertTrue(evidence['complete'])
        self.assertTrue(evidence['available'])
        self.assertFalse(manifest.degraded)
        self.assertEqual([p['id'] for p in evidence['posts']], ['finding', 'reply'])
        self.assertEqual(evidence['posts'][0]['content'], finding['message'])
        self.assertEqual(evidence['posts'][1]['thread_id'], 'finding')
        self.assertEqual(evidence['excluded_generated_assessments'], 1)
        self.assertEqual(get.call_args_list[0].kwargs['auth_scheme'], 'Bearer')
        self.assertIn('https://chat.example/main/pl/finding', evidence['posts'][0]['url'])

    @patch.dict(os.environ, {}, clear=True)
    @patch('swf_epicprod.assessment.bundle._get')
    def test_missing_credential_is_a_visible_limitation(self, get):
        manifest = _Manifest()
        evidence = _live_findings(self.now, 1, manifest)
        get.assert_not_called()
        self.assertFalse(evidence['available'])
        self.assertTrue(manifest.degraded)

    @patch.dict(os.environ, {'MATTERMOST_TOKEN': 'test-token'}, clear=True)
    @patch('swf_epicprod.assessment.bundle.LIVE_MAX_PAGES', 1)
    @patch('swf_epicprod.assessment.bundle._get')
    def test_page_limit_is_not_mistaken_for_complete_evidence(self, get):
        post = self.post('finding', 'A current finding')
        get.side_effect = [{'id': 'team'}, {'id': 'channel'},
                           {'order': ['finding'], 'posts': {'finding': post}}]
        manifest = _Manifest()
        evidence = _live_findings(self.now, 1, manifest)
        self.assertFalse(evidence['complete'])
        self.assertTrue(manifest.degraded)
        self.assertEqual(evidence['posts'][0]['content'], post['message'])

    @patch.dict(os.environ, {'MATTERMOST_TOKEN': 'test-token'}, clear=True)
    @patch('swf_epicprod.assessment.bundle._get', side_effect=OSError('unavailable'))
    def test_failed_read_is_not_mistaken_for_no_resolution(self, get):
        manifest = _Manifest()
        evidence = _live_findings(self.now, 1, manifest)
        self.assertFalse(evidence['available'])
        self.assertTrue(manifest.degraded)
        self.assertIn('report that limitation', evidence['assessment_requirement'])

    def test_findings_are_present_in_the_human_review_bundle(self):
        page = reporting.render_bundle_page({
            'live_findings': {'posts': [{'content': 'Certificate renewal confirmed.'}]}})
        self.assertIn('Production findings and resolutions', page)
        self.assertIn('Certificate renewal confirmed.', page)

    @patch('swf_epicprod.assessment.bundle._live_findings')
    @patch('swf_epicprod.assessment.bundle._get', return_value={})
    def test_scheduled_assembly_includes_the_channel_read(self, get, findings):
        findings.return_value = {'complete': True, 'posts': [{'content': 'Resolved.'}]}
        bundle = assemble('26.07', 'daily', 1, monitor_url='https://monitor.example',
                          corun_url='https://corun.example', corun_token='test-token')
        findings.assert_called_once()
        self.assertEqual(bundle['live_findings'], findings.return_value)


if __name__ == '__main__':
    unittest.main()
