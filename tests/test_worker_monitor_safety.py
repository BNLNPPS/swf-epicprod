"""Execute the actual Harvester failure paths and PanDA dispatch guard.

HARVESTER_SAFETY_SOURCE names a captured installed pandaharvester directory;
the sibling read-only reference checkout is the development default. Database,
scheduler and logging boundaries are mocked. No production calls are made.
"""
import ast
import copy
import datetime
import importlib.util
import itertools
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any
import unittest
import time
from unittest.mock import Mock
from concurrent.futures import ThreadPoolExecutor

ROOT = Path(__file__).resolve().parents[1]


def load_script(name):
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), ROOT / 'scripts' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def function_node(source, name, parent=None):
    tree = ast.parse(source)
    nodes = tree.body
    if parent:
        nodes = next(n for n in nodes if isinstance(n, ast.ClassDef) and n.name == parent).body
    return copy.deepcopy(next(n for n in nodes if isinstance(n, ast.FunctionDef) and n.name == name))


def compile_node(node, namespace):
    node.decorator_list = []
    tree = ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[]))
    exec(compile(tree, '<captured Harvester source>', 'exec'), namespace)
    return namespace[node.name]


class WorkerMonitorSafetyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.preparer = load_script('prepare-harvester-monitor-safety')
        candidate = Path(os.environ.get('HARVESTER_SAFETY_SOURCE', ROOT.parents[1] / 'harvester' / 'pandaharvester'))
        if not candidate.is_dir():
            raise unittest.SkipTest('Set HARVESTER_SAFETY_SOURCE to a captured or reference pandaharvester package')
        cls.sources = {rel: (candidate / rel).read_text() for rel in cls.preparer.PATCHES}
        cls.patched = cls.preparer.prepare_sources(cls.sources)

    def setUp(self):
        self.logger = Mock()
        self.work = SimpleNamespace(workerID=17, submissionHost='LOCAL', batchID='1.0',
                                    status='running', nativeStatus='unknown',
                                    checkTime=datetime.datetime(2026, 10, 25, 10),
                                    set_pilot_error=Mock(), set_supplemental_error=Mock())

    def timeout_result(self, preserve=True):
        source = self.patched['harvesterbody/monitor.py']
        tree = ast.parse(source)
        # Execute the real unknown-status branch, including its timeout check.
        branch = next(n for n in ast.walk(tree) if isinstance(n, ast.If)
                      and ast.unparse(n.test) == 'newStatus is None'
                      and any(isinstance(x, ast.Attribute) and x.attr == 'mark_workers_to_kill_by_workerids'
                              for x in ast.walk(n)))
        node = ast.parse('def probe():\n    pass').body[0]
        node.body = [copy.deepcopy(branch), ast.parse('return newStatus').body[0]]
        db = SimpleNamespace(mark_workers_to_kill_by_workerids=Mock())
        ns = {'newStatus': None, 'diagMessage': 'scheduler state unknown',
              'workSpec': self.work, 'workerID': 17, 'retMap': {17: {}},
              'mon_core': SimpleNamespace(preserveUnknownWorkers=preserve),
              'timeNow': datetime.datetime(2026, 10, 25, 12), 'checkTimeout': 3600,
              'self': SimpleNamespace(dbProxy=db), 'tmp_log': self.logger,
              'datetime': datetime, 'WorkSpec': SimpleNamespace(ST_cancelled='cancelled'),
              'PilotErrors': SimpleNamespace(FAILEDBYSERVER=1)}
        # Variables assigned inside the extracted branch must be function locals.
        node.args.args = [ast.arg(arg=k) for k in ns]
        result = compile_node(node, {})(**ns)
        return result, db

    def test_scheduler_absence_beyond_timeout_does_not_kill_running_worker(self):
        result, db = self.timeout_result()
        self.assertEqual(result, 'running')
        db.mark_workers_to_kill_by_workerids.assert_not_called()
        self.work.set_pilot_error.assert_not_called()
        self.logger.error.assert_called_once()

    def test_scheduler_absence_beyond_timeout_does_not_kill_submitted_worker(self):
        self.work.status = 'submitted'
        result, db = self.timeout_result()
        self.assertEqual(result, 'submitted')
        db.mark_workers_to_kill_by_workerids.assert_not_called()

    def test_other_monitor_timeout_policy_is_preserved(self):
        result, db = self.timeout_result(preserve=False)
        self.assertEqual(result, 'cancelled')
        db.mark_workers_to_kill_by_workerids.assert_called_once_with([17])

    def test_query_exception_is_failure_not_successful_empty_result(self):
        source = self.patched['harvestermonitor/htcondor_monitor.py']
        node = function_node(source, 'check_workers', 'HTCondorMonitor')
        query = Mock(side_effect=RuntimeError('schedd unavailable'))
        ns = {'baseLogger': self.logger, 'CondorJobQuery': query,
              'get_host_batchid_map': lambda _: {'LOCAL': {'1.0': self.work}},
              'Pool': ThreadPoolExecutor, '_check_one_worker': lambda *a, **k: (None, 'not found')}
        monitor = SimpleNamespace(make_logger=lambda *a, **k: self.logger,
                                  cacheEnable=False, cacheRefreshInterval=30,
                                  useCondorHistory=True, useCondorHistoryMaxAge=86400,
                                  nProcesses=1, cancelUnknown=False, heldTimeout=3600,
                                  payloadType=None)
        ok, reason = compile_node(node, ns)(monitor, [self.work])
        self.assertFalse(ok)
        self.assertIn('schedd unavailable', reason)
        self.assertEqual(self.work.status, 'running')

    def test_shell_query_failure_does_not_become_missing_worker(self):
        node = function_node(self.patched['harvestermisc/htcondor_utils.py'], 'query_with_command', 'CondorJobQuery')
        ns = {'baseLogger': self.logger, 'core_utils': SimpleNamespace(make_logger=lambda *a, **k: self.logger),
              '_runShell': lambda _: (1, '', 'schedd unavailable')}
        query = SimpleNamespace(submissionHost='LOCAL', orig_comStr_list=['condor_q -xml'],
                                condor_schedd=None, condor_pool=None)
        with self.assertRaisesRegex(RuntimeError, 'schedd unavailable'):
            compile_node(node, ns)(query, {'1.0': self.work})

    def test_explicit_removed_batch_record_still_cancels(self):
        node = function_node(self.patched['harvestermonitor/htcondor_monitor.py'], '_check_one_worker')
        ns = {'baseLogger': self.logger, 'core_utils': SimpleNamespace(make_logger=lambda *a, **k: self.logger),
              'condor_job_id_from_workspec': lambda _: 'LOCAL#1.0',
              'CONDOR_JOB_STATUS_MAP': {'3': 'removed'},
              'WorkSpec': SimpleNamespace(ST_cancelled='cancelled'),
              'WorkerErrors': SimpleNamespace(htcondor_message_pattern_handler=SimpleNamespace(get_error_code=lambda _: 0),
                                              error_codes={'SUCCEEDED': 0, 'GENERAL_ERROR': 1})}
        result, _ = compile_node(node, ns)(self.work, {'LOCAL#1.0': {'JobStatus': 3}})
        self.assertEqual(result, 'cancelled')

    def test_patch_is_idempotent_and_refuses_unknown_source(self):
        self.assertEqual(self.preparer.prepare_sources(self.patched), self.patched)
        broken = dict(self.sources, **{'harvesterbody/monitor.py': 'pass\n'})
        with self.assertRaisesRegex(ValueError, 'Unsupported source'):
            self.preparer.prepare_sources(broken)

    def test_failed_query_keeps_nojob_worker_in_caller_fifo_without_advancing_checktime(self):
        """Run both real wrapper and agent core, not only the plugin boundary."""
        statuses = ['submitted', 'running', 'idle', 'ready', 'finished', 'failed', 'cancelled', 'missed']
        states = SimpleNamespace(**{'ST_' + s: s for s in statuses}, ST_LIST=statuses,
                                 MT_NoJob='NoJob', MT_MultiWorkers='MultiWorkers')
        config = SimpleNamespace(monitor=SimpleNamespace(checkTimeout=3600,
                                 fifoCheckInterval=30, checkInterval=30,
                                 fifoMaxCheckInterval=30, lockInterval=600, sleepTime=600))
        ns = {'datetime': datetime, 'itertools': itertools, 'time': time,
              'WorkSpec': states, 'harvester_config': config, '_logger': self.logger,
              'core_utils': SimpleNamespace(naive_utcnow=lambda: datetime.datetime(2026, 10, 25, 12),
                                            dump_error_message=Mock())}
        source = self.patched['harvesterbody/monitor.py']
        wrapper = compile_node(function_node(source, 'check_workers', 'Monitor'), ns)
        core = compile_node(function_node(source, 'monitor_agent_core', 'Monitor'), ns)
        for status in ['running', 'submitted']:
            with self.subTest(status=status):
                work = Mock()
                work.workerID = 17
                work.status = status
                work.hasJob = 0
                work.mapType = 'NoJob'
                work.nJobsToReFill = None
                work.workAttributes = {'existing': 'preserved'}
                work.checkTime = datetime.datetime(2026, 10, 25, 10)
                original_check = work.checkTime
                work.has_work_params.return_value = False
                work.get_work_params.return_value = (False, None)
                work.is_post_processed.return_value = False
                work.has_updated_attributes.return_value = True
                work.set_status.side_effect = lambda s: setattr(work, 'status', s)
                work.set_work_attributes.side_effect = lambda attrs: setattr(work, 'workAttributes', attrs)
                mon = SimpleNamespace(check_workers=lambda _: (False, 'schedd unavailable'))
                messenger = Mock()
                messenger.job_requested.return_value = 0
                queue = SimpleNamespace(monitor='monitor', messenger='messenger')
                db = Mock()
                db.lock_workers.return_value = True
                db.update_jobs_workers.return_value = True
                agent = SimpleNamespace(make_logger=lambda *a, **k: self.logger,
                     queueConfigMapper=SimpleNamespace(has_queue=lambda *a: True, get_queue=lambda *a: queue),
                     pluginFactory=SimpleNamespace(get_plugin=lambda name: mon if name == 'monitor' else messenger),
                     dbProxy=db, monitor_fifo=SimpleNamespace(enabled=True), startTimestamp=time.time() - 10000)
                agent.check_workers = lambda *args: wrapper(agent, *args)
                result = core(agent, 'test-lock', 'OSG', [[work]], from_fifo=True)
                self.assertIsNotNone(result)
                enqueued, head, _, interval = result
                self.assertEqual(enqueued, [[work]])
                self.assertEqual(head, [])
                self.assertEqual(interval, 30)
                self.assertEqual(work.status, status)
                self.assertEqual(work.checkTime, original_check)
                self.assertEqual(work.workAttributes, {'existing': 'preserved'})
                db.mark_workers_to_kill_by_workerids.assert_not_called()
                db.update_jobs_workers.assert_called_once()
                self.assertIn('WORKER_STATE_UNVERIFIED', work.set_dialog_message.call_args.args[0])


class PanDADispatchGuardTest(unittest.TestCase):
    def setUp(self):
        self.preparer = load_script('prepare-panda-worker-dispatch-guard')
        nodes = ast.parse('def probe(self, harvester_id, worker_id, tmp_log, comment, retJobs, nSent):\n    pass').body[0]
        body = ast.parse(self.preparer.NEW.lstrip().replace('\n                                ', '\n'))
        nodes.body = body.body + ast.parse('return "allowed"').body
        self.guard = compile_node(nodes, {})
        self.proxy = SimpleNamespace(conn=SimpleNamespace(begin=Mock()),
                                     cur=Mock(), _rollback=Mock())

    def run_guard(self, status, harvester='harvester', worker=17):
        self.proxy.cur.fetchone.return_value = None if status is None else (status,)
        return self.guard(self.proxy, harvester, worker, Mock(), '', ['already acquired'], 3)

    def test_every_terminal_parent_refuses_new_dispatch_without_changing_prior_jobs(self):
        for status in ['finished', 'failed', 'cancelled', 'missed']:
            with self.subTest(status=status):
                self.assertEqual(self.run_guard(status), (['already acquired'], 3))
                self.assertIn('FOR UPDATE', self.proxy.cur.execute.call_args.args[0])
        self.assertEqual(self.proxy._rollback.call_count, 4)

    def test_live_and_new_parent_keep_registration_semantics(self):
        for status in ['running', 'submitted', 'idle', None]:
            with self.subTest(status=status):
                self.assertEqual(self.run_guard(status), 'allowed')
        self.proxy._rollback.assert_not_called()

    def test_non_harvester_request_does_not_query_parent(self):
        self.assertEqual(self.run_guard(None, None, None), 'allowed')
        self.proxy.cur.execute.assert_not_called()

    def test_unavailable_parent_query_does_not_allow_dispatch(self):
        self.proxy.cur.execute.side_effect = RuntimeError('database unavailable')
        with self.assertRaisesRegex(RuntimeError, 'database unavailable'):
            self.run_guard('running')

    def test_full_captured_getjobs_refuses_terminal_parent_before_candidate_mutation(self):
        path = os.environ.get('PANDA_SAFETY_SOURCE')
        if not path:
            self.skipTest('Set PANDA_SAFETY_SOURCE to captured job_complex_module.py')
        original = Path(path).read_text()
        source = self.preparer.prepare_source(original)
        node = next(n for n in ast.walk(ast.parse(source)) if isinstance(n, ast.FunctionDef) and n.name == 'getJobs')
        node = copy.deepcopy(node)
        for arg in node.args.args:
            arg.annotation = None
        node.returns = None
        entity = SimpleNamespace(get_config_for_pq=lambda _: {'workflow': 'pull', 'meanrss': 0},
                                 getSortingCriteria=lambda *a: ('', {}))
        ns = {'naive_utcnow': lambda: datetime.datetime(2026, 10, 25, 12),
              'datetime': datetime, 'copy': copy, 'panda_config': SimpleNamespace(),
              'get_entity_module': lambda _: entity, 'Any': Any}
        get_jobs = compile_node(node, ns)
        args = dict(nJobs=1, siteName='OSG', prodSourceLabel='managed', mem=4096,
                    diskSpace=0, node='worker', timeout=60, computingElement=None,
                    prodUserID=None, taskID=None, background=False, resourceType='SCORE',
                    harvester_id='harvester', worker_id=17, schedulerID=None,
                    jobType=None, is_gu=False, via_topic=False, remaining_time=None,
                    target_architecture=None)
        for status in ['finished', 'failed', 'cancelled', 'missed']:
            with self.subTest(status=status):
                proxy = Mock()
                proxy.getConfigValue.return_value = False
                proxy.construct_where_clause.return_value = (' WHERE jobStatus=:status', {':status': 'activated'})
                proxy.cur.fetchall.return_value = [(123, 100, '', 456)]
                proxy.cur.fetchone.return_value = (status,)
                proxy._commit.return_value = True
                self.assertEqual(get_jobs(proxy, **args), ([], 0))
                queries = [c.args[0] for c in proxy.cur.execute.call_args_list]
                self.assertEqual(len(queries), 2)
                self.assertIn('Harvester_Workers', queries[1])
                self.assertIn('FOR UPDATE', queries[1])
                self.assertFalse(any(q.lstrip().startswith(('UPDATE', 'INSERT')) for q in queries))
                proxy._rollback.assert_called_once()


if __name__ == '__main__':
    unittest.main()
