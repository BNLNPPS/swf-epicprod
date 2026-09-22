#!/usr/bin/env python3
"""request-events-distribution.py — the requested-event distribution from
a terminal, and the PNG of it for a message.

The reading and the distribution are ``swf_epicprod.request_events``,
which the Requested events plot page renders from its nightly cached
product; this script prints the same numbers with an audit table of every
request and how its answer was read, and draws the PNG when asked.

Run::

    cd /data/wenauseic/github/swf-monitor/src
    source ~/.env
    /data/wenauseic/github/swf-testbed/.venv/bin/python \
        ../../swf-epicprod/scripts/request-events-distribution.py \
        [--out plot.png] [--csv table.csv] [--quiet]
"""
import argparse
import csv
import os
import sys

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(THIS_DIR, '..', '..', 'swf-monitor', 'src'))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')

import django  # noqa: E402
django.setup()

from swf_epicprod.request_events import (  # noqa: E402
    COST_LOW_S, COST_MAX_S, COST_MEDIAN_S, OFF_SCALE,
    core_hours, gather, human, percentiles, summarize,
)


def plot(values, path, extreme_label=None):
    """Two panels over one log axis: how many requests ask for how much,
    and the share of requests at or below a threshold. Separate panels
    rather than two scales on one, since the count and the share are
    different measures."""
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import numpy as np

    surface, ink, muted, series = '#fcfcfb', '#0b0b0b', '#52514e', '#2a78d6'
    ordered = np.array(sorted(values), dtype=float)
    low, high = 1e4, OFF_SCALE
    offscale = [v for v in ordered if v > high]
    shown = np.array([v for v in ordered if v <= high])
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
             f'{len(ordered)} requests stating a number; median '
             f'{human(float(np.median(ordered)))}, '
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

    for p, value in percentiles(list(ordered), (50, 75, 90)).items():
        for a in (ax, cum):
            a.axvline(value, color=muted, linewidth=1, linestyle=(0, (4, 3)))
        ax.annotate(f'p{p}\n{human(value)}', xy=(value, 1),
                    xycoords=('data', 'axes fraction'),
                    xytext=(4, -16), textcoords='offset points',
                    color=ink, fontsize=9.5, va='top')

    # The cost axis: the same numbers as core-hours at the median
    # measured cost. A rescale of the one axis, not a second scale.
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
             'count can differ six-fold in cost.\nSource: the ePIC production request '
             "record in PCS; a free-text answer is read from the requester's own words.",
             color=muted, fontsize=8.5, ha='left')
    fig.savefig(path, facecolor=surface)
    return path


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--out', help='write the plot here (PNG)')
    ap.add_argument('--csv', help='write the audit table here')
    ap.add_argument('--quiet', action='store_true', help='the numbers only')
    args = ap.parse_args()

    rows = gather()
    summary = summarize(rows)
    stated = [r for r in rows if r.get('events')]
    values = [r['events'] for r in stated]

    if not args.quiet:
        print(f"{'when':11} {'events':>10}  {'answer':<40} note")
        for r in rows:
            print(f"{str(r.get('when') or ''):11} {human(r.get('events')):>10}  "
                  f"{(r.get('answer') or '')[:40]:<40} {r.get('note', '')}")
        print()
    print(f"requests: {summary['requests']}; stating a number: {summary['stated']}; "
          f"unstated: {summary['unstated']}")
    print('percentiles: ' + ', '.join(f'p{p} {human(v)}'
                                      for p, v in summary['percentiles'].items()))
    print(f"min {human(summary['min'])}, max {human(summary['max'])}, "
          f"total requested {human(summary['total'])}")
    print(f"thresholds over the {summary['body']} requests at or below "
          f"{human(OFF_SCALE)}:")
    for t in summary['thresholds']:
        print(f"  above {human(t['threshold']):>4}: {t['requests']:3} requests "
              f"({t['share']:4.1f}%), {human(t['events'])} events, "
              f"{t['core_hours']:,.0f} core-hours at the median cost")
    for r in summary['apart']:
        print(f"apart: {human(r['events'])} on {r['when']} — {r['what'][:60]} "
              f"({core_hours(r['events']):,.0f} core-hours at the median cost, "
              f"were it a simulation event)")

    if args.csv:
        with open(args.csv, 'w', newline='') as f:
            fields = ['when', 'key', 'answer', 'events', 'note', 'what']
            writer = csv.DictWriter(f, fieldnames=fields, extrasaction='ignore')
            writer.writeheader()
            writer.writerows(rows)
        print(f'table: {args.csv}')
    if args.out:
        extreme = max(stated, key=lambda r: r['events'])
        what = ', '.join((extreme.get('what') or '').split(',')[:2]).strip()
        label = f"one request for {human(extreme['events'])} — {what}"
        print(f'plot: {plot(values, args.out, extreme_label=label)}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
