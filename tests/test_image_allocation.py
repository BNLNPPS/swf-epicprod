"""Waiting budget and mixed-image demand never retag running supply."""
import importlib.util
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

path = Path(__file__).resolve().parents[1] / 'pilot/native/image-allocation.py'
spec = importlib.util.spec_from_file_location('allocation', path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class ImageAllocationTests(unittest.TestCase):
    def test_waiting_is_one_shared_budget(self):
        result = module.targets(['one', 'three'], {'one': 900, 'three': 100}, {}, 600)
        self.assertEqual(sum(result.values()), 600)
        self.assertGreater(result['one'], result['three'])
        self.assertGreater(result['three'], 0)

    def test_running_cohort_changes_the_waiting_split(self):
        empty = module.targets(['one', 'three'], {'one': 1000, 'three': 1000}, {}, 600)
        old = module.targets(['one', 'three'], {'one': 1000, 'three': 1000}, {'one': 1000}, 600)
        self.assertGreater(old['three'], empty['three'])
        self.assertEqual(sum(old.values()), 600)

    def test_no_demand_and_small_canary_demand_do_not_fill_the_budget(self):
        self.assertEqual(module.targets(['one', 'three'], {}, {}, 600), {'one': 0, 'three': 0})
        self.assertEqual(module.targets(['one', 'three'], {'three': 2}, {}, 600), {'one': 0, 'three': 2})
        result = module.targets(['one', 'three'], {'one': 10000, 'three': 1}, {'one': 2000}, 600)
        self.assertEqual(result['three'], 1)

    def test_protected_canary_and_capacity_edge_cases(self):
        self.assertEqual(module.targets(['one', 'three'], {}, {}, 1, {'three': 1}), {'one': 0, 'three': 1})
        for budget in (0, 1, 2, 599, 600):
            result = module.targets(['one', 'three'], {'one': 4500, 'three': 500}, {'one': 1000}, budget)
            self.assertLessEqual(sum(result.values()), budget)
            self.assertLessEqual(result['three'], 500)

    def test_restart_surplus_is_parked_without_touching_running_workers(self):
        images = ['/cvmfs/one', '/cvmfs/three']
        ads = [dict(ClusterId=1, ProcId=n, QDate=n, JobStatus=1,
                    SingularityImage=images[0], EpicprodNativeImage=images[0],
                    EpicprodImageReady=True) for n in range(605)]
        ads.append(dict(ClusterId=2, ProcId=0, QDate=0, JobStatus=2,
                        SingularityImage=images[0], EpicprodNativeImage=images[0]))
        demand = dict(queue='BNL_OSG_PanDA_1',
                      observed_at=module.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'),
                      images=[dict(image=i, activated=1000) for i in images])
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'template').write_text('request_memory = 6000\nrequest_disk = 4500000\n')
            (root / 'env').write_text('SWF_MONITOR_URL=https://example.invalid\nSWF_REPORT_TOKEN=test\n')
            policy = dict(schema_version=1, queue='BNL_OSG_PanDA_1', images=images,
                          waiting_limit=600, demand_max_age_seconds=120,
                          state_valid_seconds=360, template_file=str(root / 'template'))
            (root / 'policy').write_text(json.dumps(policy))
            args = SimpleNamespace(policy=str(root / 'policy'), env=str(root / 'env'),
                                   state=str(root / 'state'), apply=True)
            with patch.dict('sys.modules', {'classad2': SimpleNamespace(parseAds=lambda _: ads)}), \
                    patch.object(module, 'command', return_value='ads'), \
                    patch.object(module.urllib.request, 'urlopen',
                                 return_value=io.BytesIO(json.dumps(demand).encode())), \
                    patch.object(module, 'edit_image', return_value=True) as edit, \
                    patch('sys.stdout', new=io.StringIO()):
                module.run(args)
            state = json.loads((root / 'state').read_text())
        self.assertEqual(state['waiting'], 605)
        self.assertEqual(sum(state['waiting_targets'].values()), 600)
        self.assertEqual(state['running'], {images[0]: 1})
        changes = {call.args[0]['ProcId']: call.args[2] for call in edit.call_args_list}
        self.assertEqual(sum(ready is False for ready in changes.values()), 5)
        self.assertTrue(all(call.args[0]['JobStatus'] == 1 for call in edit.call_args_list))


if __name__ == '__main__':
    unittest.main()
