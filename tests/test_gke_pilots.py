"""The GKE pilot cycle's pure parts (docs/GKE_PILOT_FLOW.md)."""
import os
import tempfile
import unittest
from types import SimpleNamespace as Obj
from unittest.mock import Mock, patch

import yaml

from swf_epicprod.gke_pilots import (DEFAULTS, PLACEHOLDER, TEMPLATE_DEFAULT,
                                     NATIVE_RUNTIME, blockers, decide, pod_supply, render_job,
                                     run_cycle)


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
        cfg = dict(DEFAULTS, credential='/nonexistent', native_runtime='/nonexistent')
        reasons = blockers(cfg, text + PLACEHOLDER)
        self.assertIn('gke_pilots.enabled is false', reasons)
        self.assertIn('gke_pilots.max_pods is 0', reasons)
        self.assertTrue(any('native runtime unavailable' in r for r in reasons))

    def test_ready(self):
        with tempfile.TemporaryDirectory() as root, tempfile.NamedTemporaryFile() as kc:
            for name in ('epic-native-pilot.sh', 'pilot3-native.tar.gz'):
                open(os.path.join(root, name), 'w').close()
            os.mkdir(os.path.join(root, 'python'))
            cfg = dict(DEFAULTS, enabled=True, namespace='epic', max_pods=4,
                       credential=kc.name, native_runtime=root, es_channel=root)
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
        self.assertIn(NATIVE_RUNTIME, c['args'][0])
        self.assertIn('EPICPROD_NATIVE_BATCH=1', c['args'][0])
        self.assertIn('ATHENA_PROC_NUMBER=13', c['args'][0])
        self.assertIn('EPICPROD_ES_CHANNEL=', c['args'][0])
        self.assertTrue(c['args'][0].startswith('install -m 600 /proxy/x509up /pilotdir/x509up'))
        self.assertIn('-q BNL_ePIC_GOOGLE_es', c['args'][0])
        self.assertNotIn('--resource-type', c['args'][0])
        secrets = [v['secret']['secretName'] for v in pod['spec']['volumes'] if 'secret' in v]
        self.assertEqual(secrets, ['s1'])
        self.assertNotIn('resources', template['spec']['template']['spec']['containers'][0])


class SupplyTest(unittest.TestCase):
    def test_jobs_without_pods_fill_the_cap(self):
        core, batch = Mock(), Mock()
        core.list_namespaced_pod.return_value.items = []
        batch.list_namespaced_job.return_value.items = [
            Obj(metadata=Obj(name=f'job-{i}'), status=Obj(conditions=[])) for i in range(5)]
        pending, running = pod_supply(core, batch, DEFAULTS)
        self.assertEqual((pending, running), (5, 0))
        self.assertEqual(decide(50, pending, running, 5)[0], 0)

    def test_running_orphan_still_uses_a_slot(self):
        core, batch = Mock(), Mock()
        core.list_namespaced_pod.return_value.items = [
            Obj(metadata=Obj(name='pod', owner_references=[Obj(kind='Job', name='deleted-job')]),
                status=Obj(phase='Running'))]
        batch.list_namespaced_job.return_value.items = []
        self.assertEqual(pod_supply(core, batch, DEFAULTS), (0, 1))

    def test_overlapping_cycle_does_not_start_work(self):
        import fcntl
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, SWF_TMP_DIR=root):
            with open(os.path.join(root, 'gke-pilot-cycle.lock'), 'a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with patch('swf_epicprod.gke_pilots._run_cycle') as cycle:
                    self.assertEqual(run_cycle()['outcome'], 'held')
                    cycle.assert_not_called()


if __name__ == '__main__':
    unittest.main()
