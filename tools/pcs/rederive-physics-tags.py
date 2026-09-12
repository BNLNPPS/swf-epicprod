#!/usr/bin/env python3
"""rederive-physics-tags.py: bring physics tags up to the path parser.

A physics tag's parameters are derived from the production path when its
dataset is taken in (pcs/physics_match.py, ``derive_physics``). When the
parser learns an axis it did not read before, tags made earlier keep the
parameters they were made with: the twelve BeAGLE eHe3 tags of 26.07 were
made without ``beam_species`` and sent PBEAM=166 instead of 166_He3, so
the payload asked for a detector geometry the image does not carry and
exited 83 at its geometry check. This tool re-derives every tag from its
datasets' recorded PanDA task names and reports each tag whose derivation
differs; with --apply it corrects the tags whose difference is purely an
axis the tag lacked, and leaves alone any tag whose corrected parameters
would collide with another tag's, or whose recorded axes would change.

Django-bootstrap standalone script against the deployed monitor:

    /opt/swf-monitor/current/.venv/bin/python rederive-physics-tags.py [--apply] [--tag LABEL ...]

SWF_MONITOR_SRC names the monitor source tree (default
/opt/swf-monitor/current/src).
"""
import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.environ.get('SWF_MONITOR_SRC', '/opt/swf-monitor/current/src'))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')

import django  # noqa: E402
django.setup()

from pcs.models import Dataset, PhysicsTag  # noqa: E402
from pcs.physics_match import derive_physics, taskname_remainder_path  # noqa: E402
from pcs.services import _physics_key  # noqa: E402


def taskname_remainder(taskname):
    """The descriptive remainder of a PanDA task name:
    group . EIC . <det_version> . <det_config> . <remainder...>"""
    parts = taskname.split('.')
    if len(parts) < 7 or parts[0] != 'group' or parts[1] != 'EIC':
        return ''
    if not re.fullmatch(r'\d+\.\d+\.\d+', '.'.join(parts[2:5])):
        return ''
    return '.'.join(parts[6:])


def derived_for(dataset):
    """The parser's reading of the dataset's recorded task name, or None."""
    source = ((dataset.metadata or {}).get('source') or {})
    if source.get('kind') != 'panda_taskname':
        return None
    remainder = taskname_remainder(str(source.get('location') or ''))
    if not remainder:
        return None
    beam_match = re.search(r'\.(\d+x\d+)(\.|$)', remainder)
    beam = beam_match.group(1) if beam_match else ''
    derived = derive_physics(taskname_remainder_path(remainder), beam=beam)
    if derived is None or derived.get('process') in ('BEAMGAS', 'SYNRAD'):
        return None
    return derived


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--apply', action='store_true', help='correct the additive differences')
    ap.add_argument('--tag', action='append', default=[], help='only this tag label (repeatable)')
    args = ap.parse_args()

    by_key = {}
    for tag in PhysicsTag.objects.all():
        by_key.setdefault(_physics_key(tag.parameters), []).append(tag)

    report = []
    seen = set()
    qs = Dataset.objects.filter(physics_tag__isnull=False).select_related('physics_tag')
    if args.tag:
        qs = qs.filter(physics_tag__tag_label__in=args.tag)
    for ds in qs.order_by('physics_tag__tag_number', 'id'):
        tag = ds.physics_tag
        derived = derived_for(ds)
        if derived is None:
            continue
        current = dict(tag.parameters or {})
        if _physics_key(derived) == _physics_key(current):
            continue
        if (tag.tag_label, json.dumps(derived, sort_keys=True)) in seen:
            continue
        seen.add((tag.tag_label, json.dumps(derived, sort_keys=True)))
        changed = {k: (current.get(k, ''), derived.get(k, '')) for k in set(current) | set(derived)
                   if current.get(k, '') != derived.get(k, '')}
        additive = all(old == '' for old, _new in changed.values())
        collision = [t.tag_label for t in by_key.get(_physics_key(derived), []) if t.pk != tag.pk]
        entry = {'tag': tag.tag_label, 'status': tag.status,
                 'datasets': Dataset.objects.filter(physics_tag=tag).count(),
                 'source': ((ds.metadata or {}).get('source') or {}).get('location'),
                 'changes': changed, 'additive': additive, 'collides_with': collision,
                 'action': 'skip'}
        if additive and not collision:
            entry['action'] = 'apply' if args.apply else 'would apply'
            if args.apply:
                tag.parameters = derived
                tag.save(update_fields=['parameters', 'updated_at'])
                by_key.setdefault(_physics_key(derived), []).append(tag)
        report.append(entry)

    for e in report:
        print(f"{e['tag']:<8} {e['status']:<7} datasets {e['datasets']:<3} {e['action']:<12} "
              f"{e['changes']}" + (f" collides with {e['collides_with']}" if e['collides_with'] else '')
              + (f"  [{e['source']}]" if e['action'] == 'skip' else ''))
    applied = sum(1 for e in report if e['action'] == 'apply')
    print(json.dumps({'differing': len(report), 'applied': applied,
                      'skipped': sum(1 for e in report if e['action'] == 'skip')}))
    return 0


if __name__ == '__main__':
    sys.exit(main())
