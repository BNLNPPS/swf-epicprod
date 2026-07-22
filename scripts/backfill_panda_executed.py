"""Backfill executed software identity onto existing PanDA associations.

The association reconciler now records each PanDA task's executed
container(s) — immutable execution evidence from the PanDA job records —
as ``PandaTasks.metadata['executed']``. This one-off fills the rows
associated before that capture existed.

Run under the venv with the swf-monitor project on the path:

    cd <swf-monitor>/src && source <venv>/bin/activate && source ~/.env
    python <swf-epicprod>/scripts/backfill_panda_executed.py [--apply]

Dry-run by default; --apply writes.
"""

import argparse
import os
import sys

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')

import django  # noqa: E402

django.setup()

from pcs.models import PandaTasks  # noqa: E402
from pcs.services import _panda_executed_identity  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true',
                        help='write the change (default: dry-run report)')
    args = parser.parse_args()

    rows = (PandaTasks.objects
            .filter(jedi_task_id__isnull=False)
            .order_by('jedi_task_id'))
    filled, empty, present = 0, 0, 0
    for row in rows:
        if isinstance((row.metadata or {}).get('executed'), dict):
            present += 1
            continue
        identity = _panda_executed_identity(row.jedi_task_id)
        if not identity:
            empty += 1
            print(f'  task {row.jedi_task_id} ({row.task_name[-50:]}): '
                  f'no container recorded on its jobs')
            continue
        filled += 1
        print(f'  task {row.jedi_task_id}: '
              f'{identity["container_image"].rsplit("/", 1)[-1]}')
        if args.apply:
            row.metadata = {**(row.metadata or {}), 'executed': identity}
            row.save(update_fields=['metadata', 'updated_at'])
    verb = 'filled' if args.apply else 'would fill'
    print(f'{verb} {filled}; already present {present}; '
          f'no container evidence {empty}')
    if not args.apply:
        print('Dry run — rerun with --apply to write.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
