"""Refuse image mismatches before task mutation, allocation or submission."""
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase
from pcs import services
from pcs.api_views import ProdTaskViewSet
from swf_epicprod import queues

IMAGE1 = '/cvmfs/singularity.opensciencegrid.org/eicweb/eic_xl:26.07.1-stable'
IMAGE3 = '/cvmfs/singularity.opensciencegrid.org/eicweb/eic_xl:26.07.3-stable'


class ServiceImageAdmissionTests(SimpleTestCase):
    def setUp(self):
        for patcher in (
            patch('swf_epicprod.front.closed_queues', return_value={}),
            patch('swf_epicprod.front.regulated_queues', return_value=['OSG']),
            patch.object(queues, 'dispatch_capabilities', return_value={
                'OSG': {'mode': 'bound', 'images': [IMAGE1]}}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.task = SimpleNamespace(
            get_effective_config=lambda: {'container_image': IMAGE3, 'panda_site': 'OSG'},
            panda_task_id=None, overrides={'panda_site': 'OSG'},
            dataset=SimpleNamespace(metadata={}), save=Mock())

    def test_manual_placement_refused_before_save(self):
        with self.assertRaisesRegex(services.ServiceError, 'needs image'):
            services.prodtask_place(self.task, 'OSG')
        self.task.save.assert_not_called()

    def test_saved_placement_refused_before_attempt_allocated(self):
        with patch.object(services, 'prodtask_allocate_panda_tasks') as allocate:
            with self.assertRaisesRegex(services.ServiceError, 'needs image'):
                services.prodtask_submit_request(task=self.task)
            allocate.assert_not_called()
        self.task.save.assert_not_called()

    def test_queued_submission_refused_before_mark_saved(self):
        with self.assertRaisesRegex(services.ServiceError, 'needs image'):
            services.prodtask_queue_submission(task=self.task, kind='submit')
        self.task.save.assert_not_called()

    def test_final_doer_admission_checks_the_actual_overridden_image(self):
        view = ProdTaskViewSet()
        view.get_queryset = Mock(return_value=None)
        with patch.object(services, 'resolve_prodtask', return_value=self.task):
            for image, expected in [(IMAGE3, 409), (IMAGE1, 200)]:
                request = SimpleNamespace(query_params={
                    'name': 'task', 'fmt': 'admission', 'site': 'OSG', 'container': image})
                result = view.command(request)
                self.assertEqual(result.status_code, expected, result.data)
                if expected == 409:
                    self.assertIn(IMAGE3, result.data['detail'])

