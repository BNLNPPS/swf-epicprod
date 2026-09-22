"""The requested-event distribution as inline SVG: how many requests ask
for how many events, and the share of requests at or below a size, over
one logarithmic axis carrying a second reading in core-hours
(docs/REQUEST_SIZES.md).

Two panels rather than two scales on one axis: a count and a share are
different measures. No script, and legible in both themes — the marks
carry their own colour, everything else follows the page's text colour.
"""
import math

from django.utils.html import escape
from django.utils.safestring import mark_safe

WIDTH = 980
LEFT = 64
RIGHT = 24
TOP = 46           # the core-hours caption and its ticks
BARS_H = 210
GAP = 46           # the shared axis between the panels
CUM_H = 110
BOTTOM = 34

BAR = '#2a78d6'
MARK = '#b2560d'   # the percentile guides, distinct from the bars


def _x(value, low, high):
    """The pixel of a value on the log axis."""
    span = math.log10(high / low)
    return LEFT + (WIDTH - LEFT - RIGHT) * math.log10(value / low) / span


def _decades(low, high):
    d = int(math.floor(math.log10(low)))
    while 10 ** d <= high:
        if 10 ** d >= low:
            yield 10 ** d
        d += 1


def _label(value):
    for scale, suffix in ((1e9, 'B'), (1e6, 'M'), (1e3, 'k')):
        if value >= scale:
            text = f'{value / scale:.1f}'.rstrip('0').rstrip('.')
            return f'{text}{suffix}'
    return f'{value:.0f}'


def distribution_svg(summary):
    """The SVG for a summary from swf_epicprod.request_events.summarize,
    or '' when it holds nothing to draw."""
    bins = [b for b in (summary.get('bins') or []) if b.get('hi')]
    steps = summary.get('cumulative') or []
    if not bins or not steps:
        return ''
    low = bins[0]['lo']
    high = bins[-1]['hi']
    tallest = max(b['count'] for b in bins) or 1
    height = TOP + BARS_H + GAP + CUM_H + BOTTOM
    bars_bottom = TOP + BARS_H
    cum_top = bars_bottom + GAP
    cum_bottom = cum_top + CUM_H
    cost = summary.get('cost') or {}
    per_event = float(cost.get('median_s') or 0) or None

    out = [f'<svg viewBox="0 0 {WIDTH} {height}" width="100%" '
           f'style="max-width:{WIDTH}px;height:auto;font-size:11px" '
           f'role="img" aria-label="Distribution of requested events per request">']

    # The panels' frames and horizontal guides.
    out.append(f'<line x1="{LEFT}" y1="{bars_bottom}" x2="{WIDTH - RIGHT}" '
               f'y2="{bars_bottom}" stroke="currentColor" stroke-opacity="0.45"/>')
    out.append(f'<line x1="{LEFT}" y1="{cum_bottom}" x2="{WIDTH - RIGHT}" '
               f'y2="{cum_bottom}" stroke="currentColor" stroke-opacity="0.45"/>')

    # The count axis: a guide every whole number when few, else rounded.
    step = 1 if tallest <= 8 else (5 if tallest <= 40 else 10)
    value = step
    while value <= tallest:
        y = bars_bottom - BARS_H * value / tallest
        out.append(f'<line x1="{LEFT}" y1="{y:.1f}" x2="{WIDTH - RIGHT}" y2="{y:.1f}" '
                   f'stroke="currentColor" stroke-opacity="0.12"/>')
        out.append(f'<text x="{LEFT - 8}" y="{y + 3:.1f}" text-anchor="end" '
                   f'fill="currentColor" fill-opacity="0.75">{value}</text>')
        value += step
    out.append(f'<text transform="translate(16,{TOP + BARS_H / 2:.0f}) rotate(-90)" '
               f'text-anchor="middle" fill="currentColor" fill-opacity="0.75">'
               f'requests</text>')

    # The bars, one per half-decade, with a 2px surface gap.
    for b in bins:
        if not b['count']:
            continue
        x0 = _x(b['lo'], low, high)
        x1 = _x(b['hi'], low, high)
        h = BARS_H * b['count'] / tallest
        out.append(
            f'<rect x="{x0 + 1:.1f}" y="{bars_bottom - h:.1f}" '
            f'width="{max(1.0, x1 - x0 - 2):.1f}" height="{h:.1f}" fill="{BAR}">'
            f'<title>{b["count"]} request{"s" if b["count"] != 1 else ""} '
            f'from {escape(_label(b["lo"]))} to {escape(_label(b["hi"]))}</title></rect>')

    # The share at or below, as a step line.
    points = []
    for point in steps:
        x = _x(min(max(point['value'], low), high), low, high)
        y = cum_bottom - CUM_H * point['share'] / 100.0
        if points:
            points.append(f'{x:.1f},{points[-1].split(",")[1]}')
        points.append(f'{x:.1f},{y:.1f}')
    out.append(f'<polyline fill="none" stroke="currentColor" stroke-width="2" '
               f'points="{" ".join(points)}"/>')
    for share in (25, 50, 75, 90, 100):
        y = cum_bottom - CUM_H * share / 100.0
        out.append(f'<line x1="{LEFT}" y1="{y:.1f}" x2="{WIDTH - RIGHT}" y2="{y:.1f}" '
                   f'stroke="currentColor" stroke-opacity="0.12"/>')
        out.append(f'<text x="{LEFT - 8}" y="{y + 3:.1f}" text-anchor="end" '
                   f'fill="currentColor" fill-opacity="0.75">{share}%</text>')
    out.append(f'<text transform="translate(16,{cum_top + CUM_H / 2:.0f}) rotate(-90)" '
               f'text-anchor="middle" fill="currentColor" fill-opacity="0.75">'
               f'at or below</text>')

    # The shared axis of requested events, decade by decade.
    for value in _decades(low, high):
        x = _x(value, low, high)
        out.append(f'<line x1="{x:.1f}" y1="{TOP}" x2="{x:.1f}" y2="{bars_bottom}" '
                   f'stroke="currentColor" stroke-opacity="0.10"/>')
        out.append(f'<line x1="{x:.1f}" y1="{cum_top}" x2="{x:.1f}" y2="{cum_bottom}" '
                   f'stroke="currentColor" stroke-opacity="0.10"/>')
        out.append(f'<text x="{x:.1f}" y="{bars_bottom + 16:.1f}" text-anchor="middle" '
                   f'fill="currentColor" fill-opacity="0.85">{escape(_label(value))}</text>')
        if per_event:
            hours = value * per_event / 3600.0
            out.append(f'<text x="{x:.1f}" y="{TOP - 8:.1f}" text-anchor="middle" '
                       f'fill="currentColor" fill-opacity="0.6">'
                       f'{escape(_label(hours))}</text>')
    out.append(f'<text x="{WIDTH - RIGHT}" y="{cum_bottom + 24:.1f}" text-anchor="end" '
               f'fill="currentColor" fill-opacity="0.75">requested events</text>')
    if per_event:
        out.append(f'<text x="{LEFT}" y="12" text-anchor="start" '
                   f'fill="currentColor" fill-opacity="0.6">core-hours at '
                   f'{per_event} CPU s/event, the median measured</text>')

    # The percentiles, through both panels.
    for row, (name, value) in enumerate(sorted(
            (summary.get('percentiles') or {}).items(), key=lambda kv: int(kv[0]))):
        if not value or value < low or value > high:
            continue
        x = _x(value, low, high)
        label_y = TOP + 12 + 14 * (row % 2)
        out.append(f'<line x1="{x:.1f}" y1="{TOP}" x2="{x:.1f}" y2="{cum_bottom}" '
                   f'stroke="{MARK}" stroke-width="1" stroke-dasharray="4 3" '
                   f'stroke-opacity="0.8"/>')
        # A halo in the page's own background, so the label reads over a
        # bar as well as over the surface, in either theme.
        out.append(f'<text x="{x + 4:.1f}" y="{label_y:.1f}" fill="{MARK}" '
                   f'style="paint-order:stroke;stroke:var(--bs-body-bg,#ffffff);'
                   f'stroke-width:3px;stroke-linejoin:round">'
                   f'p{escape(name)} {escape(_label(value))}</text>')

    out.append('</svg>')
    return mark_safe(''.join(out))
