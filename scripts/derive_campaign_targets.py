"""Derive campaign-included target-events candidates from recorded
request material (CAMPAIGN_DELIVERY.md delivery step 1, the curation
pass).

Sources, in confidence order:

1. PC-anchored production request ``nevents`` (structured integers) —
   proposed at the ``requested`` tier.
2. Questionnaire ``nevents`` free text, attributed through each
   edition's recorded questionnaire matches and parsed (k/M/B suffixes,
   'Million', scientific notation, 'N x M' multipliers, comma lists
   mapped onto multi-edition matches) — proposed at the ``requested``
   tier with a per-row confidence flag. Unparseable texts are listed,
   never silently dropped.

EVGEN input file counts are deliberately not converted to events: no
recorded events-per-file exists for these inputs, so no number is
stated (the events/job configuration gap is CAMPAIGN_DELIVERY.md
extension 2).

Dry-run prints the review table; --apply writes the confident rows
through ``dataset_expected_events_set`` (one action-stream event) and
never writes rows flagged for review.

Run under the venv with the swf-monitor project on the path:

    cd <swf-monitor>/src && source <venv>/bin/activate && source ~/.env
    python <swf-epicprod>/scripts/derive_campaign_targets.py \\
        --campaign 26.07 [--apply]
"""

import argparse
import os
import re
import sys

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')

import django  # noqa: E402

django.setup()

from pcs.models import Dataset, ProdTask, Questionnaire  # noqa: E402
from pcs.services import (dataset_expected_events_set,  # noqa: E402
                          pc_request_projection)

NUMBER = re.compile(
    r'(\d+(?:\.\d+)?)(?:\s*[eE]\s*\+?\s*(\d+))?\s*'
    r'(k|m|b|g|million|billion)?\b', re.IGNORECASE)
MULTIPLIER = re.compile(r'x\s*(\d+)\b', re.IGNORECASE)
SCALE = {'k': 1e3, 'm': 1e6, 'million': 1e6,
         'b': 1e9, 'g': 1e9, 'billion': 1e9}


def parse_counts(text):
    """Ordered event counts found in a questionnaire nevents text.

    Returns (counts, note): counts as integers; note flags structure
    worth a human glance ('multiplier', 'list'). Empty counts means
    unparseable."""
    cleaned = text.replace(',', ' , ')
    counts = []
    for match in NUMBER.finditer(cleaned):
        value = float(match.group(1))
        if match.group(2):
            value *= 10 ** int(match.group(2))
        unit = (match.group(3) or '').lower()
        if unit:
            value *= SCALE[unit]
        elif value < 1000:
            # A bare small number ('the 4 Q2 bins', '3 samples') is
            # structure, not an event count.
            continue
        counts.append(int(value))
    note = ''
    if MULTIPLIER.search(text):
        note = 'multiplier'
    elif len(counts) > 1:
        note = 'list'
    return counts, note


def build(campaign):
    heads = list(
        Dataset.objects.filter(campaign__name=campaign)
        .select_related('physics_config', 'physics_tag')
        .order_by('composed_name', 'block_num', 'pk')
        .distinct('composed_name'))
    projection = pc_request_projection(heads)

    matches = {}
    for name, overrides in (ProdTask.objects
                            .filter(campaign__name=campaign)
                            .values_list('dataset__composed_name',
                                         'overrides')):
        for m in (overrides or {}).get('questionnaire_matches') or []:
            qid = m.get('questionnaire_id') if isinstance(m, dict) else None
            if qid:
                matches.setdefault(name, []).append(int(qid))
    questionnaires = {q.pk: q for q in Questionnaire.objects.filter(
        pk__in={q for qs in matches.values() for q in qs})}

    confident, review, unparsed = [], [], []
    for head in heads:
        name = head.composed_name
        if head.expected_events is not None:
            continue  # already curated; never overwrite
        anchored = [r.nevents for r in projection.get(name, ())
                    if r.nevents]
        if anchored:
            confident.append({'name': name, 'value': max(anchored),
                              'source': 'requested',
                              'why': f'anchored request ({max(anchored)})'})
            continue
        for qid in matches.get(name, []):
            text = (questionnaires[qid].nevents or '').strip()
            if not text:
                continue
            counts, note = parse_counts(text)
            if not counts:
                unparsed.append({'name': name, 'qid': qid, 'text': text})
                continue
            # 'total of 15M' distributes across the matched editions —
            # a human split, never a per-edition auto-write.
            if 'total' in text.lower() and not note:
                note = 'total'
            row = {'name': name, 'qid': qid, 'text': text, 'note': note,
                   'source': 'requested', 'value': counts[0],
                   'counts': counts}
            if note or len(counts) > 1:
                review.append(row)
            else:
                row['why'] = f'q#{qid} {text!r}'
                confident.append(row)
            break
    return confident, review, unparsed


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--campaign', default='26.07')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()

    confident, review, unparsed = build(args.campaign)
    print(f'campaign {args.campaign}: {len(confident)} confident, '
          f'{len(review)} flagged for review, {len(unparsed)} unparseable')
    print('\n== confident (written by --apply) ==')
    for row in confident:
        print(f"  {row['name'].split('craterlake.')[-1]:42s} "
              f"{row['value']:>14,d}  {row['why']}")
    print('\n== flagged for review (never auto-written) ==')
    for row in review:
        print(f"  {row['name'].split('craterlake.')[-1]:42s} "
              f"counts={row['counts']} note={row['note']} "
              f"q#{row['qid']} {row['text'][:60]!r}")
    print('\n== unparseable ==')
    for row in unparsed:
        print(f"  {row['name'].split('craterlake.')[-1]:42s} "
              f"q#{row['qid']} {row['text'][:70]!r}")

    if not args.apply:
        print('\ndry run — nothing written; --apply writes the '
              'confident rows')
        return
    entries = [{'name': row['name'], 'expected_events': row['value'],
                'source': row['source']} for row in confident]
    if not entries:
        print('nothing to write')
        return
    result = dataset_expected_events_set(
        entries,
        f'Derived from production request material for {args.campaign}: '
        'anchored request counts and parsed questionnaire counts '
        '(scripts/derive_campaign_targets.py)',
        changed_by='wenaus')
    print(f"\napplied: changed {len(result['changed'])}, "
          f"unchanged {len(result['unchanged'])}, "
          f"unknown {result['unknown']}")


if __name__ == '__main__':
    sys.exit(main())
