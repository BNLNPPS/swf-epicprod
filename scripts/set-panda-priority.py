#!/usr/bin/env python3
"""Apply an approved priority decision with exact task/job guards.

The caller first verifies the decision and operator through the broker, per
EPICPROD_OPS.md. Keep the decision JSON outside Git: task_id, job_id,
task_name, site, owner, old_priority, new_priority, requested_by, reason,
decision_message_id, decision_operator_id. This input is operational evidence,
not a grant of authority by itself.

Run from the deployed monitor environment with the normal environment loaded.
Default is read-only. --apply records the decision before one authenticated
PanDA task/change_priority call, then verifies both task and queued job.
No PCS priority level, request priority, site setting or other task is changed.
"""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

TASK = JOB = NAME = SITE = MESSAGE = OPERATOR = OLD = NEW = DECISION = None


def inside(apply):
    from pandaclient import Client

    def snapshot():
        status, result = Client.get_task_details_json(TASK)
        if status != 0 or not isinstance(result, (tuple, list)) or not result[0]:
            raise RuntimeError('Task detail read failed')
        row = {str(k).lower(): v for k, v in result[1].items()}
        status, jobs = Client.getJobStatus([JOB])
        if status != 0 or not jobs or jobs[0] is None:
            raise RuntimeError('Job detail read failed')
        job = jobs[0]
        return {'task': {'id': row['jeditaskid'], 'name': row['taskname'],
            'owner': row['username'], 'status': row['status'],
            'current_priority': row.get('currentpriority'),
            'initial_priority': row.get('taskpriority')},
            'job': {'id': job.PandaID, 'task_id': job.jediTaskID,
                'owner': job.prodUserName, 'site': job.computingSite,
                'status': job.jobStatus, 'current_priority': job.currentPriority,
                'assigned_priority': job.assignedPriority}}

    before = snapshot()
    task, job = before['task'], before['job']
    if not (task['id'] == TASK and task['name'] == NAME and task['owner'] == DECISION['owner']
            and job['id'] == JOB and job['task_id'] == TASK and job['owner'] == DECISION['owner']
            and job['site'] == SITE):
        raise RuntimeError('Exact target identity or owner does not match the decision')
    report = {'task_name': NAME, 'decision_message_id': MESSAGE, 'before': before,
              'changed': False, 'verified': False}
    if not apply:
        return report
    if task['current_priority'] == job['current_priority'] == NEW:
        return {**report, 'after': before, 'verified': True, 'reason': 'already at requested priority'}
    if not (task['current_priority'] == job['current_priority'] == OLD
            and task['status'] == 'running' and job['status'] == 'activated'):
        raise RuntimeError('Target state or priority changed; no priority write was sent: ' + json.dumps(before))

    @Client.curl_request_decorator(endpoint='task/change_priority', method='post',
                                   json_out=True, output_mode='full')
    def change_priority(task_id, priority, verbose=False):
        return {'task_id': task_id, 'priority': priority}

    # Exactly one write. Any uncertain response must be resolved by readback,
    # never by automatically sending the priority operation again.
    status, result = change_priority(TASK, NEW)
    after = snapshot()
    report.update(after=after, transport_status=status, response=result,
                  write_attempted=True,
                  changed=status == 0 and isinstance(result, dict) and result.get('success') is True,
                  verified=after['task']['current_priority'] == after['job']['current_priority'] == NEW)
    return report


def main():
    global TASK, JOB, NAME, SITE, MESSAGE, OPERATOR, OLD, NEW, DECISION
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--decision', type=Path, required=True)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--inside-pclient', choices=('read', 'apply'))
    args = parser.parse_args()
    DECISION = json.loads(args.decision.read_text())
    TASK, JOB = int(DECISION['task_id']), int(DECISION['job_id'])
    NAME, SITE = DECISION['task_name'], DECISION['site']
    MESSAGE, OPERATOR = DECISION['decision_message_id'], DECISION['decision_operator_id']
    OLD, NEW = int(DECISION['old_priority']), int(DECISION['new_priority'])
    if not all((TASK, JOB, NAME, SITE, MESSAGE, OPERATOR, DECISION['owner'],
                DECISION['requested_by'], DECISION['reason'])):
        raise RuntimeError('Decision identity and provenance fields are required')
    if args.inside_pclient:
        try:
            report = inside(args.inside_pclient == 'apply')
        except Exception as error:
            print(json.dumps({'error': str(error), 'decision_message_id': MESSAGE}))
            return 1
        print(json.dumps(report, default=str))
        return 0 if not args.inside_pclient == 'apply' or report['verified'] else 1

    audit = None
    requested = None
    if args.apply:
        sys.path.insert(0, '/opt/swf-monitor/current/src')
        os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')
        import django
        django.setup()
        from monitor_app.epicprod_logging import log_epicprod_action

        def audit(outcome, report):
            return log_epicprod_action('manual', 'panda_task_operation',
                subject_type='panda_task', subject_key=str(TASK), username='wenaus',
                outcome=outcome, operation='change_priority', task_name=NAME,
                jedi_task_id=TASK, panda_id=JOB, decision_message_id=MESSAGE,
                decision_operator_id=OPERATOR, requested_by=DECISION['requested_by'],
                reason=DECISION['reason'],
                report=report, message=f'{NAME}: current priority {OLD} -> {NEW}, '
                    f'{outcome}; {DECISION["requested_by"]} TeamComms decision {MESSAGE}')
        requested = audit('requested', {'from': OLD, 'to': NEW})
        if requested is None:
            raise RuntimeError('Decision could not be recorded; no priority write was sent')
    command = ['bash', '-c',
        'set -e; source "$1"; export PANDA_AUTH_VO=EIC.production; exec python3 "$2" --decision "$3" --inside-pclient "$4"',
        'panda-priority', str(Path.home() / 'pclient/run/setup.sh'), str(Path(__file__).resolve()), str(args.decision.resolve()),
        'apply' if args.apply else 'read']
    completed = subprocess.run(command, capture_output=True, text=True, timeout=90)
    lines = [line for line in completed.stdout.splitlines() if line.startswith('{')]
    if not lines:
        if audit:
            audit('error', {'error': 'No structured result', 'returncode': completed.returncode})
        raise RuntimeError('No structured PanDA result; do not resend without checking task and job')
    report = json.loads(lines[-1])
    if audit:
        event_id = audit('ok' if completed.returncode == 0 and report.get('verified') else 'error', report)
        report.update(request_audit_id=requested, result_audit_id=event_id)
        if event_id is None:
            report['audit_error'] = 'Completion audit failed; requested decision remains recorded'
    print(json.dumps(report, default=str))
    return completed.returncode


if __name__ == '__main__':
    raise SystemExit(main())
