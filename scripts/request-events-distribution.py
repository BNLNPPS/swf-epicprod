#!/usr/bin/env python3
"""request-events-distribution.py — how many events the ePIC production
requests ask for, as a distribution.

Written for Markus Diefenthaler's question of 2026-09-22: the
distribution of requested event counts over the production requests that
state one, to set a threshold above which a request is extraordinary and
needs a motivation before it is prioritized.

The population is the production-request Google Form, mirrored in PCS as
``pcs.models.Questionnaire`` — one row per submitted request, with the
requester's own free-text answer for the number of events ("10M",
"5M x 3", "total of 15M", "1M in each energy range"). ``parse_events``
turns that text into one number per request and says how it read it, and
the audit table prints every row so a reader can check the reading
rather than trust it. A request that states no number is counted as
unstated and left out of the distribution.

The cost axis: an event is not a unit of work. The measured CPU cost of
simulation plus reconstruction, per event, over the production record
(the canary node measurement store) spans a factor of six across physics
configurations, so the plot carries a second axis in core-hours at the
median measured cost and states the range.

Run::

    cd /data/wenauseic/github/swf-monitor/src
    source ../../swf-testbed/.venv/bin/activate && source ~/.env
    python ../../swf-epicprod/scripts/request-events-distribution.py \
        [--out /path/to/plot.png] [--csv /path/to/table.csv]

Prints the audit table and the percentiles; writes the plot when asked.
"""
import argparse
import csv
import os
import re
import sys

THIS_DIR = os.path.dirname(os.path.abspath(__file__))

# The measured cost of one event, CPU seconds of simulation plus
# reconstruction, from the canary node measurement store over the
# production record (2026-09-22): the well-sampled configurations run
# 2.6 to 5.9, and the whole measured set reaches 16.4.
COST_LOW_S = 2.6
COST_MEDIAN_S = 3.5
COST_HIGH_S = 5.9
COST_MAX_S = 16.4
# Beyond this the request is not a simulation event count in the
# ordinary sense (the ten billion synchrotron photons of SynradG4),
# so it is reported apart rather than folded into the sums.
OFF_SCALE = 1e8

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
    cleaned = BEAM.sub(' ', plain)
    lowered = cleaned.lower()

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
        note += '; per range or per file, no total stated — lower bound'
    return sum(parts), note


def rows():
    """Every mirrored request, newest first, with its reading. The only
    part that needs the platform: the parsing above stands alone and is
    tested without it."""
    sys.path.insert(0, os.path.join(THIS_DIR, '..', '..', 'swf-monitor', 'src'))
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')
    import django
    django.setup()
    from pcs.models import Questionnaire
    out = []
    for q in Questionnaire.objects.order_by('-submitted_at'):
        events, note = parse_events(q.nevents)
        out.append({
            'submitted': q.submitted_at.date().isoformat(),
            'answer': ' '.join((q.nevents or '').split()),
            'events': events,
            'note': note,
            'description': ' '.join((q.description or '').split())[:80],
        })
    return out


def percentiles(values, points=(50, 75, 90, 95)):
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
    if value is None:
        return '-'
    for scale, suffix in ((1e9, 'B'), (1e6, 'M'), (1e3, 'k')):
        if value >= scale:
            text = f'{value / scale:.1f}'.rstrip('0').rstrip('.')
            return f'{text}{suffix}'
    return f'{value:.0f}'


def plot(values, path, extreme_label=None):
    """Two panels over one log axis: how many requests ask for how much,
    and the share of requests at or below a threshold. Separate panels
    rather than two scales on one (the counts and the share are different
    measures)."""
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import numpy as np

    surface, ink, muted, series = '#fcfcfb', '#0b0b0b', '#52514e', '#2a78d6'
    ordered = np.array(sorted(values), dtype=float)
    # The axis covers where the requests are. One request, ten billion
    # synchrotron photons, sits three decades beyond the rest; carrying
    # the axis out to it would spend half the width on empty space, so
    # it is named at the edge instead of drawn to scale.
    low, high = 1e4, OFF_SCALE
    offscale = [v for v in ordered if v > high]
    shown = np.array([v for v in ordered if v <= high])
    # Half-decade bins: the answers themselves are round numbers a
    # half-decade apart (500k, 1M, 2M, 5M, 10M).
    edges = np.logspace(np.log10(low), np.log10(high),
                        int(round(2 * np.log10(high / low))) + 1)

    fig, (ax, cum) = plt.subplots(
        2, 1, figsize=(10, 7.6), sharex=True, dpi=200,
        gridspec_kw={'height_ratios': [2, 1], 'hspace': 0.14})
    fig.subplots_adjust(top=0.80, bottom=0.16, left=0.10, right=0.97)
    fig.patch.set_facecolor(surface)

    for a in (ax, cum):
        a.set_facecolor(surface)
        a.set_xscale('log')
        a.grid(axis='y', color='#e3e2de', linewidth=0.8)
        a.set_axisbelow(True)
        for side in ('top', 'right'):
            a.spines[side].set_visible(False)
        for side in ('left', 'bottom'):
            a.spines[side].set_color('#c9c8c3')
        a.tick_params(colors=muted, labelsize=9)

    ax.hist(shown, bins=edges, color=series, edgecolor=surface, linewidth=1.2)
    ax.set_ylabel('requests', color=ink, fontsize=10)
    ax.set_xlim(low, high)

    fig.text(0.10, 0.955, 'Requested events per ePIC production request',
             color=ink, fontsize=14, ha='left')
    fig.text(0.10, 0.925,
             f'{len(ordered)} requests stating a number, from the production request '
             f'form; median {human(float(np.median(ordered)))}, '
             f'{sum(1 for v in ordered if v > 1e7)} above 10M',
             color=muted, fontsize=10, ha='left')
    if offscale and extreme_label:
        fig.text(0.10, 0.897, f'Off the scale: {extreme_label}',
                 color=muted, fontsize=9.5, ha='left')

    share = np.arange(1, len(ordered) + 1) / len(ordered)
    cum.step(ordered, share * 100, where='post', color=ink, linewidth=2)
    cum.set_ylim(0, 105)
    cum.set_yticks([0, 25, 50, 75, 90, 100])
    cum.set_ylabel('% of requests\nat or below', color=ink, fontsize=10)
    cum.set_xlabel('requested events', color=ink, fontsize=10)

    marks = percentiles(list(ordered), (50, 75, 90))
    for i, (p, value) in enumerate(marks.items()):
        for a in (ax, cum):
            a.axvline(value, color=muted, linewidth=1, linestyle=(0, (4, 3)))
        ax.annotate(f'p{p}\n{human(value)}', xy=(value, 1), xycoords=('data', 'axes fraction'),
                    xytext=(4, -16), textcoords='offset points',
                    color=ink, fontsize=9.5, va='top')

    # The cost axis: the same numbers as core-hours at the median measured
    # cost of an event. A rescale of the one axis, not a second scale.
    top = ax.secondary_xaxis(
        'top', functions=(lambda e: e * COST_MEDIAN_S / 3600.0,
                          lambda h: h * 3600.0 / COST_MEDIAN_S))
    top.set_xlabel(f'core-hours at {COST_MEDIAN_S} CPU s/event, the median measured '
                   f'(simulation + reconstruction)',
                   color=muted, fontsize=9.5, labelpad=7)
    top.tick_params(colors=muted, labelsize=9)

    fig.text(0.10, 0.035,
             'An event is not a unit of work: measured CPU per event spans '
             f'{COST_LOW_S} to {COST_MAX_S} s across physics configurations, so the same '
             'count can differ six-fold in cost.\nSource: the production request form, '
             'mirrored in PCS; each answer read from the requester\'s own words.',
             color=muted, fontsize=8.5, ha='left')
    fig.savefig(path, facecolor=surface)
    return path


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--out', help='write the plot here (PNG)')
    ap.add_argument('--csv', help='write the audit table here')
    ap.add_argument('--quiet', action='store_true', help='percentiles only')
    args = ap.parse_args()

    table = rows()
    stated = [r for r in table if r['events']]
    values = [r['events'] for r in stated]

    if not args.quiet:
        print(f"{'submitted':11} {'events':>10}  {'answer':<46} note")
        for r in table:
            print(f"{r['submitted']:11} {human(r['events']):>10}  "
                  f"{r['answer'][:46]:<46} {r['note']}")
        print()
    print(f'requests mirrored: {len(table)}; stating a number: {len(stated)}; '
          f'unstated: {len(table) - len(stated)}')
    pcts = percentiles(values)
    print('percentiles: ' + ', '.join(f'p{p} {human(v)}' for p, v in pcts.items()))
    print(f'min {human(min(values))}, max {human(max(values))}, '
          f'total requested {human(sum(values))}')
    # The cost of what lies above a threshold, over the simulation
    # requests: one request for ten billion synchrotron photons is a
    # different kind of event and would otherwise be the whole sum, so
    # it is stated on its own rather than folded in.
    apart = [r for r in stated if r['events'] > OFF_SCALE]
    body = [v for v in values if v <= OFF_SCALE]
    print(f'thresholds over the {len(body)} requests at or below {human(OFF_SCALE)}:')
    for threshold in (1e6, 2e6, 5e6, 1e7, 2e7):
        over = [v for v in body if v > threshold]
        print(f'  above {human(threshold):>4}: {len(over):3} requests '
              f'({100 * len(over) / len(body):4.1f}%), '
              f'{human(sum(over))} events, '
              f'{sum(over) * COST_MEDIAN_S / 3600.0:,.0f} core-hours at the median cost')
    for r in apart:
        print(f"apart: {human(r['events'])} on {r['submitted']} — {r['description'][:60]} "
              f"({r['events'] * COST_MEDIAN_S / 3600.0:,.0f} core-hours at the median cost, "
              f"were it a simulation event)")

    if args.csv:
        with open(args.csv, 'w', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=list(table[0]))
            writer.writeheader()
            writer.writerows(table)
        print(f'table: {args.csv}')
    if args.out:
        extreme = max(stated, key=lambda r: r['events'])
        # the description's first two clauses: what it is and its generator
        what = ', '.join(extreme['description'].split(',')[:2]).strip()
        label = f"one request for {human(extreme['events'])} — {what}"
        print(f'plot: {plot(values, args.out, extreme_label=label)}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
