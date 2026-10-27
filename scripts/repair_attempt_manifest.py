"""Verify one exact sandbox manifest, and explicitly repair its stored record.

Run on the production host with its normal Django environment:
    cd <swf-monitor>/src
    <venv>/bin/python <swf-epicprod>/scripts/repair_attempt_manifest.py --jedi-taskid 41367

Default is read-only. --apply additionally requires the dry-run's --rows,
--events and --sha256, plus --reason. It changes only the attempt manifest,
retains its prior record and records an epicprod action, atomically.
"""
import argparse
import getpass
import json
import os
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--jedi-taskid', type=int, required=True)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--rows', type=int)
    parser.add_argument('--events', type=int)
    parser.add_argument('--sha256')
    parser.add_argument('--reason', default='')
    args = parser.parse_args()
    if args.apply and (args.rows is None or args.events is None or not args.sha256
                       or not args.reason.strip()):
        parser.error('--apply requires reviewed --rows, --events, --sha256 and --reason')

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')
    import django
    django.setup()
    from django.db import connection, transaction
    from monitor_app.epicprod_logging import log_epicprod_action
    from pcs import manifest_repair, manifests
    from pcs.models import PandaTasks

    try:
        with transaction.atomic():
            # Even an accidental writer in a future preparation helper must
            # not turn the default verification into a production change.
            with connection.cursor() as cursor:
                cursor.execute('SET TRANSACTION READ ONLY')
            attempt = PandaTasks.objects.get(jedi_task_id=args.jedi_taskid)
            prepared = manifest_repair.prepare(attempt)
        result = {**prepared['summary'], 'applied': False}
        if args.apply:
            actor = getpass.getuser()
            with transaction.atomic():
                locked = PandaTasks.objects.select_for_update().get(pk=attempt.pk)
                locked.metadata = manifest_repair.replacement(
                    locked.metadata, prepared, rows=args.rows, events=args.events,
                    sha256=args.sha256, actor=actor, reason=args.reason)
                locked.save(update_fields=['metadata', 'updated_at'])
                log_id = log_epicprod_action(
                    'pcs-maintenance', 'attempt_manifest_repair',
                    subject_type='panda_task', subject_key=str(args.jedi_taskid),
                    username=actor, sublevel='normal', live_default=True,
                    message=(f"Repaired {attempt.task_name} manifest: "
                             f"{result['previous_rows']} -> {result['rows']} rows; {args.reason}"),
                    **prepared['summary'], reason=args.reason)
                if log_id is None:
                    raise RuntimeError('repair action could not be recorded; transaction rolled back')
                result.update(applied=True, log_id=log_id)
        print(json.dumps(result, sort_keys=True))
    except (manifests.ManifestUnavailable, PandaTasks.DoesNotExist, ValueError, RuntimeError) as exc:
        print(f'repair refused: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
