"""Create a campaign's Standard Production ProdConfig and optionally
rebind that campaign's legacy-config drafts to it.

Clones the fullest existing Standard Production row (the 26.03.0 one
carries the PanDA submission ``data`` block the effective config feeds)
and overrides the campaign-specific identity: name, description,
jug_xl_tag and container_image (<campaign>-stable), rucio_rse per the
current live replication target (BNL-XRD).

--rebind moves the campaign's tasks still pointing at the legacy
'26.02.0 Standard Production' row onto the new config (the mechanical
placeholder uses were already migrated to the placeholder row by
migrate_placeholder_prodconfig.py, so what remains are human drafts).

Run under the venv with the swf-monitor project on the path:

    cd <swf-monitor>/src && source <venv>/bin/activate && source ~/.env
    python <swf-epicprod>/scripts/create_standard_prodconfig.py \\
        --campaign 26.06.0 [--rebind] [--apply]

Dry-run by default; --apply writes.
"""

import argparse
import os
import sys

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')

import django  # noqa: E402

django.setup()

from pcs.models import ProdConfig, ProdTask  # noqa: E402
from pcs.services import (  # noqa: E402
    STANDARD_CONFIG_RSE as RUCIO_RSE, STANDARD_CONFIG_TEMPLATE as TEMPLATE_NAME,
    ServiceError, standard_prodconfig_create, standard_prodconfig_values)

LEGACY_NAME = '26.02.0 Standard Production'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--campaign', required=True,
                        help='campaign name, e.g. 26.06.0')
    parser.add_argument('--rebind', action='store_true',
                        help="rebind the campaign's tasks still on the "
                             'legacy 26.02.0 row to the new config')
    parser.add_argument('--created-by', default='wenaus')
    parser.add_argument('--apply', action='store_true',
                        help='write the change (default: dry-run report)')
    args = parser.parse_args()

    name = f'{args.campaign} Standard Production'
    try:
        values = standard_prodconfig_values(args.campaign)
    except ServiceError as exc:
        print(f'ERROR: {exc}')
        return 1
    existing = ProdConfig.objects.filter(name=name).first()
    print(f'{"Exists" if existing else "Create"}: {name!r} '
          f'(template {TEMPLATE_NAME!r}; container '
          f'{values["container_image"]}; rucio_rse {RUCIO_RSE})')

    legacy = ProdConfig.objects.filter(name=LEGACY_NAME).first()
    rebind_qs = ProdTask.objects.none()
    if args.rebind and legacy is not None:
        rebind_qs = ProdTask.objects.filter(
            prod_config=legacy, campaign__name=args.campaign)
        by_status = {}
        for status in rebind_qs.values_list('status', flat=True):
            by_status[status] = by_status.get(status, 0) + 1
        print(f'Rebind candidates on {LEGACY_NAME!r}: '
              f'{rebind_qs.count()} — by status: {by_status}')

    if not args.apply:
        print('Dry run — rerun with --apply to write.')
        return 0

    if existing is None:
        # The executor the standard_config proposal also runs: one
        # origin-stamped action-stream event, and the campaign
        # configuration ping fulfilled when one is open.
        result = standard_prodconfig_create(
            args.campaign, changed_by=args.created_by,
            ping_title=f'Create the {args.campaign} Standard Production '
                       f'configuration')
        existing = ProdConfig.objects.get(pk=result['config_id'])
        print(f'Created config {name!r} (id {existing.pk}); '
              f'ping fulfilled: {result["ping_fulfilled"] or "none open"}.')
    else:
        print(f'Config {name!r} already exists — left unchanged.')
    if args.rebind and legacy is not None:
        updated = rebind_qs.update(prod_config=existing)
        print(f'Rebound {updated} tasks to {name!r}.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
