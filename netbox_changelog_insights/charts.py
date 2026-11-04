"""Chart data for templates. Bars are HTML; sparklines are inline SVG. No chart library."""
from datetime import timedelta

from django.utils.formats import date_format

__all__ = (
    'bar_chart',
    'sparkline',
)


def bar_chart(days, ticks=6):
    """Bars for change sets per day: height in percent of the peak, and a few date ticks."""
    peak = max((d['change_sets'] for d in days), default=0)
    step = max(1, round(len(days) / ticks))
    bars = []
    for i, d in enumerate(days):
        bars.append({
            'day': d['day'],
            'change_sets': d['change_sets'],
            'records': d['records'],
            'height': round(100 * d['change_sets'] / peak, 1) if peak else 0,
            'tick': date_format(d['day'], 'M j') if i % step == 0 else '',
            'weekend': d['day'].weekday() >= 5,
        })
    return {'bars': bars, 'peak': peak}


def sparkline(values, peak, weeks, start=None, end=None, width=96, height=24):
    """
    SVG polylines for a weekly series, scaled to a shared peak. A week that the period
    [start, end] (dates, inclusive) covers only in part has a lower count for that reason:
    the segments to its point are dashed and its title tells the days covered.
    """
    if not values:
        return None
    n = len(values)
    dx = width / max(n - 1, 1)
    pad = 2
    points = []
    for i, v in enumerate(values):
        y = height - pad - ((height - 2 * pad) * v / peak if peak else 0)
        points.append(f'{i * dx:.1f},{y:.1f}')
    covered = [7] * n
    if start is not None and end is not None:
        for i, w in enumerate(weeks):
            first, last = max(w, start), min(w + timedelta(days=6), end)
            covered[i] = max(0, (last - first).days + 1)
    partial = [c < 7 for c in covered]
    solid, dashed = [], []
    for i in range(n - 1):
        segment = f'{points[i]} {points[i + 1]}'
        (dashed if partial[i] or partial[i + 1] else solid).append(segment)
    titles = []
    for w, v, c in zip(weeks, values, covered):
        note = f' ({c} of 7 days)' if c < 7 else ''
        titles.append(f"{date_format(w, 'M j')}: {v}{note}")
    return {
        'solid': solid, 'dashed': dashed, 'width': width, 'height': height, 'title': ', '.join(titles),
    }
