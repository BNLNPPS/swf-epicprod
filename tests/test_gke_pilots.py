"""The GKE pilot cycle's pure parts (docs/GKE_PILOT_FLOW.md)."""
import os
import tempfile
import unittest

import yaml

from swf_epicprod.gke_pilots import (DEFAULTS, PLACEHOLDER, TEMPLATE_DEFAULT,
                                     WRAPPER, blockers, decide, render_job)


class DecideTest(unittest.TestCase):
    def test_one_pod_per_uncovered_job(self):
        self.assertEqual(decide(5, 1, 2, 10)[0], 4)

    def test_cap_bounds_pods_alive(self):
        n, reason = decide(20, 2, 7, 10)
        self.assertEqual(n, 1)
        n, reason = decide(20, 3, 7, 10)
        self.assertEqual((n, reason.startswith('at the cap')), (0, True))

    def test_no_backlog_starts_nothing(self):
        self.assertEqual(decide(0, 0, 4, 10), (0, 'no activated jobs'))

    def test_waiting_pods_cover_the_backlog(self):
        self.assertEqual(decide(2, 2, 0, 10)[0], 0)

    def test_zero_cap(self):
        self.assertEqual(decide(9, 0, 0, 0), (0, 'max_pods is 0'))


class BlockersTest(unittest.TestCase):
    def test_defaults_are_held(self):
        with open(TEMPLATE_DEFAULT) as f:
            text = f.read()
        cfg = dict(DEFAULTS, credential='/nonexistent')
        reasons = blockers(cfg, text + PLACEHOLDER)
        self.assertEqual(len(reasons), 4)  # enabled, max_pods, credential, template

    def test_ready(self):
        with tempfile.NamedTemporaryFile() as kc:
            cfg = dict(DEFAULTS, enabled=True, namespace='epic', max_pods=4,
                       credential=kc.name)
            self.assertEqual(blockers(cfg, 'image: x'), [])


class RenderTest(unittest.TestCase):
    def test_job_carries_queue_resources_proxy_and_wrapper(self):
        with open(TEMPLATE_DEFAULT) as f:
            template = yaml.safe_load(f.read().replace(PLACEHOLDER, 'x'))
        cfg = dict(DEFAULTS, secret_name='s1', cpu='13', memory='90Gi')
        job = render_job(template, cfg, name='epicprod-pilot-t1')
        self.assertEqual(job['metadata']['name'], 'epicprod-pilot-t1')
        pod = job['spec']['template']
        self.assertEqual(pod['metadata']['labels']['app'], 'epicprod-pilot')
        c = pod['spec']['containers'][0]
        self.assertEqual(c['resources']['limits'], {'cpu': '13', 'memory': '90Gi'})
        self.assertIn(WRAPPER, c['args'][0])
        self.assertTrue(c['args'][0].startswith('install -m 600 /proxy/x509up /pilotdir/x509up'))
        self.assertIn('-q BNL_ePIC_GOOGLE_es', c['args'][0])
        self.assertNotIn('--resource-type', c['args'][0])
        secrets = [v['secret']['secretName'] for v in pod['spec']['volumes'] if 'secret' in v]
        self.assertEqual(secrets, ['s1'])
        self.assertNotIn('resources', template['spec']['template']['spec']['containers'][0])


if __name__ == '__main__':
    unittest.main()
