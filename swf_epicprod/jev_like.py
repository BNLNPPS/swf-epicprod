"""Configurations for a request in plain words (docs/JEV.md, Plain-language
search): the configurations matching what a person types, ranked by Jev.

Word overlap narrows the catalog to ``CANDIDATES``; Jev scores each on
how well it answers the request, in one call. Asked from the find page:
the page posts the words, the production-operations agent ranks them
(doer ``jev_like``) into the cached product ``jev_like:<key>`` and
publishes ``jev_like_ready``; the page reads the stored answer. The same
words within a week are served from the store. One query is about 8,000
input tokens, about $0.0004.
"""
import hashlib

from django.utils import timezone

PRODUCT_PREFIX = 'jev_like:'
PRODUCT_TTL_S = 7 * 24 * 3600
CANDIDATES = 60
SHOWN = 20
MAX_TEXT = 1000
LEVELS = [
    'not what the request asks for',
    'related physics, a different reaction',
    'the requested process at a different beam energy or species',
    'the requested process and beam at a different kinematic range or sample variant',
    'what the request asks for, apart from the generator version, radiative setting '
    'or beam-effects variant',
    'exactly what the request asks for',
]
SHORT_LEVELS = ['not it', 'related physics', 'other beam', 'other range',
                'other variant', 'match']
INSTRUCTION = ('How well does this ePIC simulation configuration answer the request '
               'in the state?')


def normalized(text):
    return ' '.join(str(text or '').split())[:MAX_TEXT]


def key_for(text):
    return hashlib.sha1(normalized(text).lower().encode()).hexdigest()[:16]


def stored(key):
    from monitor_app.models import CachedProduct
    row = CachedProduct.objects.filter(key=PRODUCT_PREFIX + key).first()
    return (row.value if row else None) or None


def rank(text):
    """(ranked configurations, usage) for one request in plain words."""
    from pcs.models import PhysicsConfig

    from .config_neighbors import Index
    from .jev import decide

    index = Index(list(PhysicsConfig.objects.select_related('physics_tag')))
    candidates = index.ranked(text, CANDIDATES)
    questions = {c: {'type': 'score', 'instructions': {INSTRUCTION: index.fields[c]},
                     'criteria': LEVELS} for c in candidates}
    answers, usage = decide({'request': text}, questions)
    out = []
    for c in candidates:
        a = answers.get(c) or {}
        if 'score' not in a:
            continue
        probs = a.get('probabilities') or {}
        level = int(max(probs, key=lambda k: probs[k])) if probs else round(a['score'])
        out.append({'label': c, 'text': index.text[c], 'score': round(float(a['score']), 3),
                    'level': level, 'confidence': round(float(a.get('confidence') or 0), 3)})
    out.sort(key=lambda n: -n['score'])
    return out[:SHOWN], usage


def run(text, created_by='jev_like'):
    """Rank, store, record. Returns the stored value."""
    from monitor_app.cached_product import get_product
    from monitor_app.epicprod_logging import log_epicprod_action

    from .jev import JevError

    text = normalized(text)
    key = key_for(text)
    started = timezone.now()
    value = {'query': text, 'key': key, 'levels': SHORT_LEVELS, 'ranked': [], 'error': ''}
    cost = 0.0
    try:
        value['ranked'], usage = rank(text)
        cost = usage['cost_usd']
    except JevError as exc:
        value['error'] = str(exc)
    value['at'] = timezone.now().isoformat()
    get_product(PRODUCT_PREFIX + key, lambda: value, ttl_seconds=PRODUCT_TTL_S, refresh=True)
    log_epicprod_action(
        'ops-agent', 'jev_like', username=created_by,
        outcome='error' if value['error'] else 'ok',
        sublevel='normal' if value['error'] else 'low',
        message=(f'Jev search "{text[:80]}": '
                 + (value['error'] or f"{len(value['ranked'])} configurations ranked")),
        key=key, cost_usd=round(cost, 5),
        duration_s=round((timezone.now() - started).total_seconds(), 2))
    return value
