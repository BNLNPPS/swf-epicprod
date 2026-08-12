#!/usr/bin/env python3
"""resolve_slug_conflicts.py — evgen-bind the slug-sample residue.

The rows fold_slug_rows.py left as conflicts: their sample-less names
collide with a kept row because their true generator identity differs
while both carry anchor tags. Every one derives a real evgen identity
from its recorded path (``pcs.physics_config.evgen_identity``), so the
recorded group-6 disposition (PCS_COMPOSED_NAME_FAMILIES.md) applies:
bind the derived identity through ``find_or_create_evgen_tag``, clear
the slug from the sample axis (the authority's path-derived sample
stands where one exists), and recompose. A row whose recomposed name
would still collide is left untouched and reported — never guessed.

Dry-run by default; ``--apply`` executes. Django-bootstrap standalone
script — also usable by hand.

Usage::

    cd /data/wenauseic/github/swf-monitor/src
    source ../../swf-testbed/.venv/bin/activate && source ~/.env
    python ../../swf-epicprod/scripts/resolve_slug_conflicts.py [--apply]
"""
import argparse
import json
import os
import re
import sys

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(THIS_DIR, '..', '..', 'swf-monitor', 'src'))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')

import django  # noqa: E402
django.setup()

from pcs.models import Dataset  # noqa: E402
from pcs.physics_config import evgen_identity  # noqa: E402
from pcs.services import find_or_create_evgen_tag  # noqa: E402

SLUG_RE = re.compile(r'^(src-)?[0-9a-f]{12}$')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()

    rows = [d for d in Dataset.objects.select_related(
                'campaign', 'evgen_tag')
            if SLUG_RE.match(d.sample_name or '')]
    taken = set(Dataset.objects.exclude(
        pk__in=[d.pk for d in rows]).values_list('composed_name',
                                                 flat=True))
    plan = {'bind': [], 'blocked': []}
    for d in rows:
        ident, source = evgen_identity(d)
        if ident is None:
            plan['blocked'].append({'pk': d.pk,
                                    'name': d.composed_name,
                                    'reason': 'evgen unresolved'})
            continue
        generator, version, radiative = ident
        params = {'generator': generator, 'generator_version': version}
        if radiative:
            params['radiative'] = radiative
        tag, action = find_or_create_evgen_tag(
            params, created_by='slug_retraction', dry_run=not args.apply)
        entry = {'pk': d.pk, 'campaign': d.campaign.name,
                 'from': d.composed_name, 'evgen': ident,
                 'tag_action': action,
                 'tag': tag.tag_label if tag else '(new)'}
        if args.apply:
            held_tag, held_sample = d.evgen_tag, d.sample_name
            d.evgen_tag = tag
            d.sample_name = ''
            d.save()
            if d.composed_name in taken:
                d.evgen_tag, d.sample_name = held_tag, held_sample
                d.save()
                entry['reason'] = 'recomposed name still collides'
                plan['blocked'].append(entry)
                continue
            taken.add(d.composed_name)
            entry['to'] = d.composed_name
        plan['bind'].append(entry)

    print('TOTALS ' + json.dumps({k: len(v) for k, v in plan.items()}))
    for entry in plan['bind'][:8]:
        print(' bind', entry['from'][:70], '->', entry.get('to', '(dry)'),
              '| evgen', entry['evgen'], entry['tag_action'])
    for entry in plan['blocked']:
        print(' BLOCKED', entry.get('name', entry.get('from')),
              entry['reason'])
    print('mode: ' + ('APPLIED' if args.apply else 'dry-run'))
    return 0


if __name__ == '__main__':
    sys.exit(main())
