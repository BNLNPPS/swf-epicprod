#!/usr/bin/env python3
"""Record the 28 October VO verification incident; preserve original records.

Default is a read-only review. Run with the deployed monitor's Python and
environment. --apply adds one corrective audit under the configured acting
account; it changes no configuration, task, job or original log.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, '/opt/swf-monitor/current/src')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')
import django
django.setup()

from django.contrib.auth.models import User
from monitor_app.epicprod_logging import log_epicprod_action
from monitor_app.models import AppLog
from pcs.models import ProdConfig

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--apply', action='store_true')
args = parser.parse_args()
incident = 'epic-vo-verification-20261028'
ids = [n for n in range(7150902, 7150922) if n != 7150916]
rows = list(AppLog.objects.filter(pk__in=ids).order_by('pk'))
if len(rows) != 19 or any(
    (r.extra_data or {}).get('action') != 'prodconfig_vo'
    or r.extra_data.get('username') != 'ordinary'
    or r.extra_data.get('reason') != 'test'
    or r.extra_data.get('old_vo') != 'wlcg'
    or r.extra_data.get('new_vo') != 'epic' for r in rows
):
    raise RuntimeError('The exact incident records do not match; no audit written')
names = [r.extra_data['subject_key'] for r in rows]
current = dict(ProdConfig.objects.filter(name__in=names).values_list('name', 'data'))
if set(current) != set(names) or any((d or {}).get('vo') != 'epic' for d in current.values()):
    raise RuntimeError('The affected templates are not at the approved epic target')
actor = os.environ['SWF_REMOTE_CLAUDE_USER']
if not User.objects.filter(username=actor).exists():
    raise RuntimeError('Configured acting account does not exist')
prior = AppLog.objects.filter(extra_data__incident_key=incident).first()
report = {'incident_key': incident, 'corrective_of': ids, 'templates': names,
          'configured_actor': actor, 'templates_at_approved_target': True,
          'original_records_preserved': True, 'audit_id': prior.pk if prior else None}
if args.apply and prior is None:
    event = log_epicprod_action(
        'manual', 'prodconfig_vo', username=actor, outcome='error',
        incident_key=incident, corrective_of=ids, templates=names,
        summary='VO verification error: a recovered permission test changed 19 live templates before the regulator cutover.',
        reason='The test used unittest.TestCase and directly invoked a DRF action without its router permission settings.',
        narration='The executor was Codex swf-teamcomms on swf-testbed. The original records label the synthetic test identity ordinary; that was not a human operator. The user had approved the epic migration, but the verification changed configuration prematurely. JEDI generation was stopped, the shared regulator installed, old reservations allowed to expire, and generation resumed. The templates remain at the approved epic target. The tests now forbid database access and the action checks administrator access in its body.',
        sublevel='normal', live_default=False)
    if event is None:
        raise RuntimeError('Corrective audit could not be written')
    report['audit_id'] = event
print(json.dumps(report))
