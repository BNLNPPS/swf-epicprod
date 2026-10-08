"""Configurations like this one (docs/JEV.md, Neighbours): for each
physics configuration, the configurations nearest to it in physics,
ranked by Jev on one rubric.

Word overlap over the configurations' descriptions narrows the catalog
to ``CANDIDATES`` per configuration; Jev scores every candidate on the
rubric in one request. The production-operations agent computes all
configurations nightly (doer ``jev_config_neighbors``) and one on
request; the result is the cached product ``jev_config_neighbors``,
which pages read without computing. A full pass is about 1,000 calls,
about 11 million input tokens, about $0.50.
"""
import logging
import math
import re
from collections import Counter

from django.utils import timezone

logger = logging.getLogger(__name__)

PRODUCT_KEY = 'jev_config_neighbors'
PRODUCT_TTL_S = 30 * 24 * 3600
CANDIDATES = 60
SHOWN = 15
RUBRIC_VERSION = 1
# Ascending nearness, in the order a requester looking for an existing
# sample wants them: the same physics in another variant first, then the
# sibling kinematic ranges, then other beams. The top level is a
# duplicate, which the page flags.
LEVELS = [
    'unrelated physics',
    'related physics: the same broad process family, a different reaction',
    'the same process at a different beam energy or species',
    'the same process and beam, a different kinematic range or sample variant',
    'the same physics, beam and range, a different generator, generator version, '
    'radiative setting or beam-effects variant',
    'the same configuration',
]
SHORT_LEVELS = ['unrelated', 'related physics', 'other beam', 'other range',
                'other variant', 'same configuration']
INSTRUCTION = ('How near in physics is this ePIC simulation configuration to the '
               'configuration in the state?')
TOKEN_RE = re.compile(r'[a-z0-9]+(?:\.[0-9]+)*')
BEAM_EFFECTS_RE = re.compile(r'ip6_[a-z0-9_]+', re.IGNORECASE)


def tokens(text):
    return TOKEN_RE.findall(str(text or '').lower())


def config_text(pc):
    """What a configuration is, in one line: the description every Jev
    question about configurations is given."""
    summary = pc.summary() if callable(getattr(pc, 'summary', None)) else getattr(pc, 'summary', '')
    return ' | '.join(str(x) for x in (summary, pc.sample_name, pc.evgen_display,
                                       pc.config_key) if x)


def config_fields(pc):
    """The configuration as fields: its physics parameters, the generator
    with version and radiative setting, and the beam-effects variant (the
    config key's segment after the generator). Jev levels structured
    configurations more reliably than one line of text (pc434's Q² siblings,
    2026-10-07)."""
    params = dict((pc.physics_tag.parameters if pc.physics_tag_id else {}) or {})
    m = BEAM_EFFECTS_RE.search(pc.config_key or '')
    return {'physics': params, 'sample': pc.sample_name or '',
            'generator': pc.evgen_display or 'not recorded',
            'beam_effects_variant': m.group(0) if m else 'none'}


class Index:
    """Word-overlap ranking over configuration descriptions, inverse
    document frequency weighted."""

    def __init__(self, configs):
        self.text = {pc.label: config_text(pc) for pc in configs}
        self.fields = {pc.label: config_fields(pc) for pc in configs}
        self.toks = {label: set(tokens(t)) for label, t in self.text.items()}
        df = Counter()
        for ts in self.toks.values():
            df.update(ts)
        n = len(self.text)
        self.idf = {t: math.log((n + 1) / (c + 1)) + 1.0 for t, c in df.items()}

    def ranked(self, query, limit, exclude=()):
        q = set(tokens(query))
        score = {label: sum(self.idf.get(t, 0.0) for t in q & ts)
                 for label, ts in self.toks.items() if label not in exclude}
        return sorted(score, key=lambda label: -score[label])[:limit]


def neighbors_for(label, index):
    """(neighbours ranked nearest first, usage) for one configuration."""
    from .jev import decide

    candidates = index.ranked(index.text[label], CANDIDATES, exclude={label})
    questions = {c: {'type': 'score', 'instructions': {INSTRUCTION: index.fields[c]},
                     'criteria': LEVELS} for c in candidates}
    answers, usage = decide({'configuration': index.fields[label]}, questions)
    out = []
    for c in candidates:
        a = answers.get(c) or {}
        if 'score' not in a:
            continue
        probs = a.get('probabilities') or {}
        level = max(probs, key=lambda k: probs[k]) if probs else str(round(a['score']))
        out.append({'label': c, 'score': round(float(a['score']), 3),
                    'level': int(level), 'confidence': round(float(a.get('confidence') or 0), 3)})
    out.sort(key=lambda n: -n['score'])
    return out, usage


def stored():
    from monitor_app.models import CachedProduct
    row = CachedProduct.objects.filter(key=PRODUCT_KEY).first()
    return (row.value if row else None) or {}


def run(labels=None, created_by='jev_config_neighbors'):
    """Compute the neighbours of ``labels`` (all configurations when
    None), merge them into the stored product, record the run. Returns
    the summary."""
    from monitor_app.cached_product import get_product
    from monitor_app.epicprod_logging import log_epicprod_action
    from pcs.models import PhysicsConfig

    from .jev import JevError

    started = timezone.now()
    index = Index(list(PhysicsConfig.objects.select_related('physics_tag')))
    todo = [label for label in (labels or sorted(index.text)) if label in index.text]
    unknown = sorted(set(labels or []) - set(index.text))
    current = stored()
    computed = dict(current.get('computed') or {}) \
        if current.get('rubric_version') == RUBRIC_VERSION else {}
    cost, errors, done = 0.0, [], 0
    for label in todo:
        try:
            ranked, usage = neighbors_for(label, index)
        except JevError as exc:
            errors.append(f'{label}: {exc}')
            logger.error('jev neighbours %s: %s', label, exc)
            continue
        cost += usage['cost_usd']
        computed[label] = {'at': timezone.now().isoformat(), 'neighbors': ranked[:SHOWN],
                           'duplicates': [n['label'] for n in ranked if n['level'] == 5]}
        done += 1
    value = {'rubric_version': RUBRIC_VERSION, 'levels': SHORT_LEVELS,
             'candidates': CANDIDATES, 'updated_at': timezone.now().isoformat(),
             'computed': computed}
    get_product(PRODUCT_KEY, lambda: value, ttl_seconds=PRODUCT_TTL_S, refresh=True)
    summary = {'requested': len(todo), 'computed': done, 'errors': len(errors),
               'unknown_labels': unknown, 'cost_usd': round(cost, 4),
               'duplicates': sum(1 for v in computed.values() if v.get('duplicates')),
               'duration_s': round((timezone.now() - started).total_seconds(), 1)}
    log_epicprod_action(
        'ops-agent', 'jev_config_neighbors', username=created_by,
        outcome='error' if errors and not done else 'ok',
        sublevel='normal' if errors else 'low',
        message=(f'Jev neighbours: {done} of {len(todo)} configurations, '
                 f'${summary["cost_usd"]:.3f}'
                 + (f'; {len(errors)} errors, first: {errors[0]}' if errors else '')),
        **summary)
    return summary
