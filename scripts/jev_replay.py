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
import math
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections import Counter

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')

import django  # noqa: E402

django.setup()

from pcs.models import Dataset, PhysicsConfig, ProdTask, ProdRequest, Questionnaire  # noqa: E402

URL = 'https://api.typesafe.ai/v1/systemone'
MODEL = 'jev-latest'
USD_PER_INPUT_TOKEN = 42e-9
MAX_OPTIONS = 255
BANDS = ((0.9, 1.01), (0.5, 0.9), (0.0, 0.5))
TOKEN_RE = re.compile(r'[a-z0-9]+(?:\.[0-9]+)*')
INSTRUCTIONS = ('Which ePIC simulation physics configuration does this production '
                'request ask for? Match the physics process, generator and version, '
                'beam energies and species, kinematic range (Q2, angle, momentum) '
                'and sample variant.')


def tokens(text):
    return TOKEN_RE.findall(str(text or '').lower())


def pc_text(pc):
    summary = pc.summary() if callable(getattr(pc, 'summary', None)) else getattr(pc, 'summary', '')
    return ' | '.join(str(x) for x in (summary, pc.sample_name, pc.evgen_display,
                                       pc.config_key) if x)


def build_index(pcs):
    docs = {pc.label: pc_text(pc) for pc in pcs}
    df = Counter()
    toks = {}
    for label, text in docs.items():
        toks[label] = set(tokens(text))
        df.update(toks[label])
    n = len(docs)
    idf = {t: math.log((n + 1) / (c + 1)) + 1.0 for t, c in df.items()}
    return docs, toks, idf


def candidates(query, docs, toks, idf):
    q = set(tokens(query))
    ranked = sorted(docs, key=lambda label: -sum(idf.get(t, 0) for t in q & toks[label]))
    return ranked[:MAX_OPTIONS]


def ask(key, state, options, docs):
    body = {'model': MODEL, 'state': state,
            'questions': {'configuration': {
                'type': 'choice', 'instructions': INSTRUCTIONS,
                'criteria': {label: docs[label] for label in options}}}}
    req = urllib.request.Request(URL, data=json.dumps(body).encode(),
                                 headers={'Authorization': f'Bearer {key}',
                                          'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode())


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
    docs, toks, idf = build_index(list(PhysicsConfig.objects.all()))
    items = request_items if args.set == 'requests' else questionnaire_items
    out = open(args.out, 'w') if args.out else None
    tally = Counter()
    cost = 0.0
    disagreements = []
    for i, (qid, state, targets, query) in enumerate(items(args.limit)):
        if args.limit and i >= args.limit:
            break
        options = candidates(query, docs, toks, idf)
        in_candidates = bool(targets & set(options))
        # The baseline Jev is measured against: token overlap's own top pick.
        baseline_agree = options[0] in targets
        try:
            resp = ask(key, state, options, docs)
        except urllib.error.HTTPError as exc:
            print(f'{qid}: HTTP {exc.code} {exc.read().decode()[:300]}', file=sys.stderr)
            tally['http_error'] += 1
            continue
        except (OSError, ValueError) as exc:
            print(f'{qid}: {type(exc).__name__}: {exc}', file=sys.stderr)
            tally['error'] += 1
            continue
        answer = (resp.get('answers') or {}).get('configuration') or {}
        choice, conf = answer.get('choice'), float(answer.get('confidence') or 0.0)
        input_tokens = int((resp.get('usage') or {}).get('input_tokens') or 0)
        cost += input_tokens * USD_PER_INPUT_TOKEN
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
    print(f'set {args.set}: {tally["n"]} answered, errors {tally["http_error"] + tally["error"]}, '
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
