"""How many events the production requests ask for (docs/REQUEST_SIZES.md).

The reading of a request's event count, and the distribution over every
request that states one. Built nightly as the cached product the
Requested events plot page renders, so nothing is computed in a page
render; ``scripts/request-events-distribution.py`` prints the same
numbers and an audit table from a terminal.

Two record shapes carry a request's event count and both are counted,
because the question is how large the requests are, not which form they
arrived on:

- ``pcs.models.Questionnaire`` — the production request form as PCS
  mirrors it. The count is the requester's own free text ("10M",
  "5M x 3", "total of 15M", "1M in each energy range"), and
  ``parse_events`` reads it, saying how it read it.
- ``pcs.models.ProdRequest`` — the production request record, whose
  ``nevents`` is already a number.

``parse_events`` is pure and stands alone: the parsing is tested without
a database (``tests/test_request_events.py``).

An event is not a unit of work, and the page says so: the measured CPU
of simulation plus reconstruction spans 2.6 to 16.4 s per event across
physics configurations (the canary node measurement store, 2026-09-22),
so the same count differs six-fold in cost.
"""
import re

# The measured cost of one event, CPU seconds of simulation plus
# reconstruction: the well-sampled configurations run 2.6 to 5.9, and
# the whole measured set reaches 16.4.
COST_LOW_S = 2.6
COST_MEDIAN_S = 3.5
COST_HIGH_S = 5.9
COST_MAX_S = 16.4
# Beyond this a request is not a simulation event count in the ordinary
# sense (ten billion synchrotron photons through SynradG4), so it is
# named apart rather than folded into the sums and the axis.
OFF_SCALE = 1e8
THRESHOLDS = (1e6, 2e6, 5e6, 1e7, 2e7)
PERCENTILES = (50, 75, 90, 95)

UNITS = {'k': 1e3, 'm': 1e6, 'mil': 1e6, 'mill': 1e6, 'million': 1e6,
         'millions': 1e6, 'b': 1e9, 'bn': 1e9, 'billion': 1e9}
# A number with an optional unit word attached or following it.
NUMBER = re.compile(
    r'(?P<value>\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)\s*'
    r'(?P<unit>millions|million|mill|mil|bn|billion|[kKmMbB])?\b')
# A beam-energy pair (9x130, 5x41, 10x100) or a sample grid (5X3X2):
# small bare integers joined by x, with no unit on either side, so
# "12 x 1.1m" and "5M x 3" are multiplications and 9x130 is not.
BEAM = re.compile(r'(?<![\d.])\d{1,3}(?:\s*[xX]\s*\d{1,3})+'
                  r'(?![\d.]|\s*(?:k|m|b|mil|mill|million|bn|billion)\b)', re.I)
SPLIT = re.compile(r'[;,+]|\band\b')
# A comma inside a number ("900,000"), which is not a separator.
THOUSANDS = re.compile(r'(?<=\d),(?=\d{3}(?!\d))')
# Scientific notation, normalized before anything splits on its plus.
SCIENTIFIC = re.compile(r'\b(\d+(?:\.\d+)?)[eE]\s*\+?\s*(\d+)\b')


def _value(match):
    value = float(match.group('value'))
    unit = (match.group('unit') or '').lower()
    return value * UNITS.get(unit, 1.0)


def _numbers(text):
    return [_value(m) for m in NUMBER.finditer(text)]


def parse_events(text):
    """(events, note) from a requester's free-text answer, or (None, note)
    when it states no number. Pure.

    The reading, in order:
      1. an explicit total ("total of 15M", "6M events altogether",
         "(12M total)") is what the request asks for, and several stated
         totals add;
      2. otherwise the answer is split on separators and each part read:
         a multiplication ("5M x 3", "12 x 1.1m", "1000000*6") is the
         product, anything else the first number in the part, and the
         parts add;
      3. beam-energy pairs (9x130) and sample grids (5X3X2) are struck
         out first, so they are never read as counts or multipliers.
    An answer that says the count is per energy range or per file without
    stating a total is a lower bound, and the note says so.
    """
    raw = (text or '').strip()
    if not raw:
        return None, 'no answer'
    # "4.00E+06" is one number, not two either side of a plus, and the
    # comma in "900,000" is not the comma in "50K, 50K".
    plain = SCIENTIFIC.sub(lambda m: f'{float(m.group(1)) * 10 ** int(m.group(2)):.0f}', raw)
    plain = THOUSANDS.sub('', plain).replace('~', ' ')
    lowered = BEAM.sub(' ', plain).lower()

    totals = []
    for match in re.finditer(
            r'(?:total(?:ling)?\s+of\s+|totall?y\s+)?'
            r'(\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)\s*'
            r'(millions|million|mill|mil|bn|billion|[kKmMbB])?\s*'
            r'(?:events?\s+)?(?:in\s+)?(?:total|altogether)', lowered):
        totals.append(_value(re.match(NUMBER, f"{match.group(1)}{match.group(2) or ''}")))
    for pattern in (r'total\s+of\s+(\d+(?:\.\d+)?)\s*'
                    r'(millions|million|mill|mil|bn|billion|[kKmMbB])?',
                    # "... 5X3X2 samples altogether, 6M events": the total
                    # stated just after the word rather than before it.
                    r'(?:altogether|in\s+total)[^0-9]{0,12}(\d+(?:\.\d+)?)\s*'
                    r'(millions|million|mill|mil|bn|billion|[kKmMbB])?'):
        for match in re.finditer(pattern, lowered):
            totals.append(_value(re.match(NUMBER, f"{match.group(1)}{match.group(2) or ''}")))
    if totals:
        return sum(totals), f"stated total{'s' if len(totals) > 1 else ''}"

    per_unit = bool(re.search(r'\b(each|per)\b', lowered))
    parts, notes = [], []
    for part in SPLIT.split(lowered):
        if not NUMBER.search(part):
            continue
        product = re.search(
            r'(\d+(?:\.\d+)?)\s*(millions|million|mill|mil|bn|billion|[kKmMbB])?\s*'
            r'[x*×]\s*(\d+(?:\.\d+)?)\s*(millions|million|mill|mil|bn|billion|[kKmMbB])?',
            part)
        if product:
            left = _value(re.match(NUMBER, f"{product.group(1)}{product.group(2) or ''}"))
            right = _value(re.match(NUMBER, f"{product.group(3)}{product.group(4) or ''}"))
            if max(left, right) >= 1000:
                parts.append(left * right)
                notes.append('multiplied')
                continue
        parts.append(_numbers(part)[0])
    if not parts:
        return None, 'no number in the answer'
    note = 'summed parts' if len(parts) > 1 else 'single count'
    if notes:
        note = f"{note}, {notes[0]}"
    if per_unit:
        note += '; per range or per file, no total stated, a lower bound'
    return sum(parts), note


def percentiles(values, points=PERCENTILES):
    """The ordered values' percentiles, interpolated. Pure."""
    ordered = sorted(values)
    out = {}
    for p in points:
        if not ordered:
            out[p] = None
            continue
        k = (len(ordered) - 1) * p / 100.0
        low, high = int(k), min(int(k) + 1, len(ordered) - 1)
        out[p] = ordered[low] + (ordered[high] - ordered[low]) * (k - low)
    return out


def human(value):
    """A count as a person writes it: 10M, 1.8M, 650k. Pure."""
    if value is None:
        return '-'
    for scale, suffix in ((1e9, 'B'), (1e6, 'M'), (1e3, 'k')):
        if value >= scale:
            text = f'{value / scale:.1f}'.rstrip('0').rstrip('.')
            return f'{text}{suffix}'
    return f'{value:.0f}'


def core_hours(events, seconds_per_event=COST_MEDIAN_S):
    """The core-hours a count costs at a measured seconds-per-event. Pure."""
    return events * seconds_per_event / 3600.0


def histogram(values, low=1e4, high=OFF_SCALE, per_decade=2):
    """Half-decade bins over the log axis: [(lo, hi, count), ...]. Pure."""
    import math
    decades = math.log10(high / low)
    edges = [low * 10 ** (i / per_decade) for i in range(int(round(decades * per_decade)) + 1)]
    bins = []
    for lo, hi in zip(edges, edges[1:]):
        n = sum(1 for v in values if lo <= v < hi)
        bins.append({'lo': lo, 'hi': hi, 'count': n})
    if bins:
        bins[-1]['count'] += sum(1 for v in values if v == high)
    return bins


def cumulative(values):
    """[(value, share at or below as a percent)], ordered. Pure."""
    ordered = sorted(values)
    n = len(ordered)
    return [{'value': v, 'share': 100.0 * (i + 1) / n} for i, v in enumerate(ordered)]


def rows_from_records(questionnaires, prod_requests):
    """One row per request from the two record shapes, newest first.
    Pure: the records come in as dicts, so the reading is testable
    without a database.

    A record: ``{'key', 'url', 'when', 'answer', 'events', 'what'}`` —
    ``answer`` the requester's text where there is one, ``events`` the
    number where the record already holds one.
    """
    rows = []
    for record in questionnaires:
        events, note = parse_events(record.get('answer'))
        rows.append({**record, 'events': events, 'note': note})
    for record in prod_requests:
        events = record.get('events')
        rows.append({**record, 'events': float(events) if events else None,
                     'note': 'recorded count' if events else 'no count recorded'})
    rows.sort(key=lambda r: str(r.get('when') or ''), reverse=True)
    return rows


def gather():
    """Every request record that carries an event count, as rows. The
    only part that needs the platform."""
    from django.urls import reverse
    from pcs.models import ProdRequest, Questionnaire

    questionnaires = []
    for q in Questionnaire.objects.all():
        questionnaires.append({
            'key': f'questionnaire {q.pk}',
            'url': reverse('pcs:questionnaire_detail', args=[q.pk]),
            'when': q.submitted_at.date().isoformat() if q.submitted_at else '',
            'answer': ' '.join((q.nevents or '').split()),
            'what': ' '.join((q.description or '').split())[:140],
        })
    prod_requests = []
    for r in ProdRequest.objects.exclude(nevents=None):
        prod_requests.append({
            'key': f'request {r.pk}',
            'url': '',
            'when': (r.created_at.date().isoformat() if r.created_at else ''),
            'answer': str(r.nevents),
            'events': r.nevents,
            'what': ' '.join((r.description or '').split())[:140]
                    or ' '.join((r.gen_config or '').split())[:140],
        })
    return rows_from_records(questionnaires, prod_requests)


def build():
    """The cached product the page renders: the rows and the
    distribution over them, with the time it was built."""
    from django.utils import timezone
    rows = gather()
    return {'built_at': timezone.now().isoformat(),
            'rows': rows, 'summary': summarize(rows)}


def summarize(rows):
    """The distribution over the rows that state a count: the stats the
    page and the terminal both read. Pure."""
    stated = [r for r in rows if r.get('events')]
    values = [r['events'] for r in stated]
    body = [v for v in values if v <= OFF_SCALE]
    apart = [r for r in stated if r['events'] > OFF_SCALE]
    marks = percentiles(values)
    thresholds = []
    for threshold in THRESHOLDS:
        over = [v for v in body if v > threshold]
        thresholds.append({
            'threshold': threshold,
            'requests': len(over),
            'share': (100.0 * len(over) / len(body)) if body else 0.0,
            'events': sum(over),
            'core_hours': core_hours(sum(over)),
        })
    return {
        'requests': len(rows),
        'stated': len(stated),
        'unstated': len(rows) - len(stated),
        'percentiles': {str(p): v for p, v in marks.items()},
        'min': min(values) if values else None,
        'max': max(values) if values else None,
        'total': sum(values),
        'body': len(body),
        'thresholds': thresholds,
        'apart': [{'events': r['events'], 'what': r.get('what', ''),
                   'when': str(r.get('when') or ''), 'url': r.get('url', '')}
                  for r in apart],
        'bins': histogram(body),
        'cumulative': cumulative(body),
        'cost': {'low_s': COST_LOW_S, 'median_s': COST_MEDIAN_S,
                 'high_s': COST_HIGH_S, 'max_s': COST_MAX_S},
        'off_scale': OFF_SCALE,
    }
