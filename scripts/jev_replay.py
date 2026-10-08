"""Replay PCS's request-to-configuration links through Jev and measure them.

Jev (TypeSafe AI's decision model, model jev-latest) answers a Choice
question: which physics configuration a request asks for, with a
probability per candidate and a confidence. This replays two sets of
links already on the record and reports how often Jev's choice agrees
with them, by confidence band, with the cost.

- ``requests``: ProdRequests anchored to a configuration
  (``data['physics_config_anchor']``). The CSV import anchored most of
  them mechanically from the EVGEN path, so the link is exact and the
  set measures accuracy where the answer is unambiguous.
- ``questionnaires``: request-form responses, free text, with the
  configurations accepted by the earlier matchers (the Opus automatch
  and a manual Codex pass). Those are model judgments, so agreement is
  measured, not accuracy; disagreements are listed for a person.

Candidates: Choice takes at most 255 options, so each question offers
the configurations whose descriptions share the most weighted tokens
with the request; the report states how often the reference link is
among them, since a link outside the candidates cannot be chosen.

Read-only: nothing in PCS is written. Requires TYPESAFE_API_KEY. The
cost is estimated from the input tokens at $42 per billion (output
tokens are free); TypeSafe's answer reports tokens, not cost.

    cd <swf-monitor>/src && source ~/.env
    <venv>/bin/python <swf-epicprod>/scripts/jev_replay.py \\
        --set requests|questionnaires [--limit N] [--out results.jsonl]
"""

import argparse
import json
import os
import sys
import time
from collections import Counter

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')

import django  # noqa: E402

django.setup()

from pcs.models import Dataset, PhysicsConfig, ProdTask, ProdRequest, Questionnaire  # noqa: E402
from swf_epicprod.config_neighbors import Index  # noqa: E402
from swf_epicprod.jev import MAX_CHOICE_OPTIONS, JevError, decide  # noqa: E402

BANDS = ((0.9, 1.01), (0.5, 0.9), (0.0, 0.5))
INSTRUCTIONS = ('Which ePIC simulation physics configuration does this production '
                'request ask for? Match the physics process, generator and version, '
                'beam energies and species, kinematic range (Q2, angle, momentum) '
                'and sample variant.')


def ask(state, options, index):
    return decide(state, {'configuration': {
        'type': 'choice', 'instructions': INSTRUCTIONS,
        'criteria': {label: index.text[label] for label in options}}})


def label_of_anchor(anchor):
    ds = (Dataset.objects.filter(composed_name=anchor).select_related('physics_config')
          .order_by('pk').first())
    return ds.physics_config.label if ds and ds.physics_config_id else None


def request_items(limit):
    for r in ProdRequest.objects.order_by('pk'):
        anchor = (r.data or {}).get('physics_config_anchor')
        target = label_of_anchor(anchor) if anchor else None
        if not target:
            continue
        state = {'evgen_path': r.simu_path, 'generator_config': r.gen_config,
                 'background': r.background, 'description': r.description,
                 'filters': (r.data or {}).get('filters') or {}}
        yield f'request:{r.pk}', state, {target}, f'{r.simu_path} {r.gen_config} {r.description}'


def questionnaire_items(limit):
    for q in Questionnaire.objects.order_by('pk'):
        targets = set()
        for m in (q.data or {}).get('prod_matches') or []:
            if m.get('status') != 'accepted':
                continue
            task = ProdTask.objects.filter(pk=m.get('task_id')).select_related(
                'dataset__physics_config').first()
            if task and task.dataset.physics_config_id:
                targets.add(task.dataset.physics_config.label)
        if not targets:
            continue
        state = {'request': q.description, 'generator': (q.data or {}).get('generator'),
                 'events_requested': q.nevents}
        yield f'questionnaire:{q.pk}', state, targets, q.description


def band(conf):
    for lo, hi in BANDS:
        if lo <= conf < hi:
            return f'{lo:.1f}-{min(hi, 1.0):.1f}'
    return '?'


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--set', required=True, choices=('requests', 'questionnaires'))
    ap.add_argument('--limit', type=int, default=0)
    ap.add_argument('--out', default='')
    args = ap.parse_args()
    key = os.environ.get('TYPESAFE_API_KEY')
    if not key:
        print('TYPESAFE_API_KEY is not set', file=sys.stderr)
        return 2
    index = Index(list(PhysicsConfig.objects.select_related('physics_tag')))
    items = request_items if args.set == 'requests' else questionnaire_items
    out = open(args.out, 'w') if args.out else None
    tally = Counter()
    cost = 0.0
    disagreements = []
    for i, (qid, state, targets, query) in enumerate(items(args.limit)):
        if args.limit and i >= args.limit:
            break
        options = index.ranked(query, MAX_CHOICE_OPTIONS)
        in_candidates = bool(targets & set(options))
        # The baseline Jev is measured against: token overlap's own top pick.
        baseline_agree = options[0] in targets
        try:
            answers, usage = ask(state, options, index)
        except JevError as exc:
            print(f'{qid}: {exc}', file=sys.stderr)
            tally['error'] += 1
            continue
        answer = answers.get('configuration') or {}
        choice, conf = answer.get('choice'), float(answer.get('confidence') or 0.0)
        input_tokens = int(usage.get('input_tokens') or 0)
        cost += usage['cost_usd']
        agree = choice in targets
        b = band(conf)
        tally[(b, 'n')] += 1
        tally[(b, 'agree')] += int(agree)
        tally['in_candidates'] += int(in_candidates)
        tally['baseline_agree'] += int(baseline_agree)
        tally['n'] += 1
        top = sorted((answer.get('probabilities') or {}).items(), key=lambda kv: -kv[1])[:5]
        rec = {'id': qid, 'targets': sorted(targets), 'choice': choice, 'confidence': conf,
               'agree': agree, 'in_candidates': in_candidates, 'top5': top,
               'input_tokens': input_tokens}
        if out:
            out.write(json.dumps(rec) + '\n')
        if not agree:
            disagreements.append(rec)
        time.sleep(0.05)
    print(f'set {args.set}: {tally["n"]} answered, errors {tally["error"]}, '
          f'reference link among the candidates {tally["in_candidates"]}/{tally["n"]}, '
          f'cost ${cost:.4f}')
    n_all = max(tally['n'], 1)
    jev_agree = sum(tally[(f'{lo:.1f}-{min(hi, 1.0):.1f}', 'agree')] for lo, hi in BANDS)
    print(f'  Jev agrees {jev_agree}/{tally["n"]} ({100.0 * jev_agree / n_all:.0f}%); '
          f'token-overlap top pick agrees {tally["baseline_agree"]} '
          f'({100.0 * tally["baseline_agree"] / n_all:.0f}%)')
    for lo, hi in BANDS:
        b = f'{lo:.1f}-{min(hi, 1.0):.1f}'
        n = tally[(b, 'n')]
        if n:
            print(f'  confidence {b}: {n} answers, agree {tally[(b, "agree")]} '
                  f'({100.0 * tally[(b, "agree")] / n:.0f}%)')
    print(f'disagreements: {len(disagreements)}')
    for rec in disagreements[:40]:
        print(f"  {rec['id']}: chose {rec['choice']} ({rec['confidence']:.2f}), "
              f"reference {','.join(rec['targets'][:4])}"
              f"{'' if rec['in_candidates'] else ' [reference not offered]'}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
