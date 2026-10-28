"""A task cannot be placed on pilots bound to a different image."""
import unittest
from unittest.mock import patch

from swf_epicprod import queues
from swf_epicprod.front import open_to, recommend, task_need

IMAGE1 = '/cvmfs/singularity.opensciencegrid.org/eicweb/eic_xl:26.07.1-stable'
IMAGE3 = '/cvmfs/singularity.opensciencegrid.org/eicweb/eic_xl:26.07.3-stable'


def report(cap, age=1):
    return {'age_seconds': age, 'record': {'queue_status': {'pilot_dispatch': {'OSG': cap}}}}


class ImageAdmissionTests(unittest.TestCase):
    def test_exact_image_and_unknown_capability(self):
        cap = {'mode': 'bound', 'images': [IMAGE1]}
        self.assertEqual(queues.image_problem(IMAGE1, cap), '')
        self.assertIn(IMAGE3, queues.image_problem(IMAGE3, cap))
        self.assertIn(IMAGE1, queues.image_problem(IMAGE3, cap))
        self.assertTrue(queues.image_problem(IMAGE3, None))
        self.assertTrue(queues.image_problem('', {'mode': 'any'}))
        self.assertEqual(queues.image_problem(IMAGE3, {'mode': 'any'}), '')

    def test_stale_reports_and_multiple_serving_launchers(self):
        cap = {'mode': 'bound', 'images': [IMAGE1]}
        stale = queues.dispatch_capabilities({'h1': report(cap, 901)})
        self.assertIn('stale', queues.image_problem(IMAGE1, stale['OSG']))
        fresh = queues.dispatch_capabilities({'h1': report(cap),
                    'h2': report({'mode': 'bound', 'images': [IMAGE3]})})
        self.assertEqual(fresh['OSG']['images'], [IMAGE1, IMAGE3])
        self.assertEqual(queues.image_problem(IMAGE3, fresh['OSG']), '')

    def test_submission_and_placement_use_identical_image_spelling(self):
        self.assertEqual(task_need({'jug_xl_tag': '26.07.3-stable'})['container_image'], IMAGE3)

    @patch('swf_epicprod.front.closed_queues', return_value={})
    def test_incompatible_queue_never_wins_and_feed_off_still_applies(self, _closed):
        state = {'queues': {'OSG': {'feed_switch': True}, 'GREX': {'feed_switch': False}},
                 'placement': {'OSG': {'gates': {}}, 'GREX': {'gates': {}}}}
        need = dict(task_need({'container_image': IMAGE3}), rows=500)
        dispatch = {'OSG': {'mode': 'bound', 'images': [IMAGE1]}, 'GREX': {'mode': 'any'}}
        result = recommend(state, need, dispatch=dispatch)
        self.assertEqual(result['recommended'], '')
        self.assertIn(IMAGE3, result['reason'])
        state['queues']['GREX']['feed_switch'] = True
        self.assertEqual(recommend(state, need, dispatch=dispatch)['recommended'], 'GREX')
        with patch.object(queues, 'dispatch_capabilities', return_value=dispatch):
            entry = {'need': need, 'problems': [], 'level': 1, 'created_at': ''}
            self.assertEqual(open_to([entry], 'OSG', state), [])
            self.assertIn(IMAGE3, entry['unplaced_reason'])


if __name__ == '__main__':
    unittest.main()
