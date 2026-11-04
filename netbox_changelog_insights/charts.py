"""Chart data for templates. Bars are HTML; sparklines are inline SVG. No chart library."""
from datetime import timedelta

from django.utils.formats import date_format

__all__ = (
    'bar_chart',
    'heatmap',
    'sparkline',
    'stacked_chart',
)

# Categorical series: fixed order, validated for color vision deficiency in light and dark
# (CSS classes insights-s1 to insights-s6 in insights.css); "other" is gray.
SERIES = 6
WEEKDAYS = ('Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun')
HEAT_STEPS = 6


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


def sparkline(values, peak, buckets, start=None, end=None, step='week'):
    """
    Columns per day or week (step), height in percent of a shared peak (a non-zero week is never lower than
    a visible stub). A week that the period [start, end] (dates, inclusive) covers only in
    part has a lower count for that reason: it is marked and its title tells the days covered.
    """
    if not values:
        return None
    covered = [7] * len(values)
    if step == 'day':
        covered = [1] * len(values)
    elif start is not None and end is not None:
        for i, w in enumerate(buckets):
            first, last = max(w, start), min(w + timedelta(days=6), end)
            covered[i] = max(0, (last - first).days + 1)
    bars = []
    full = 1 if step == 'day' else 7
    for w, v, c in zip(buckets, values, covered):
        note = f' ({c} of 7 days)' if c < full else ''
        bars.append({
            'height': round(100 * v / peak, 1) if peak and v else 0,
            'partial': c < full,
            'title': f"{date_format(w, 'D, M j' if step == 'day' else 'M j')}: {v}{note}",
        })
    return {'bars': bars, 'step': step, 'title': ', '.join(b['title'] for b in bars)}


def stacked_chart(data, by, ticks=6):
    """
    Stacked bars of primary changes per bucket. Each segment: height in percent of the
    tallest bar, CSS class of its group. A bucket that the period covers in part is marked.
    """
    groups = data['groups']
    for i, g in enumerate(groups):
        if g['key'] == 'other':
            g['css'] = 'insights-other'
        elif by == 'action':
            g['css'] = {'create': 'bg-green', 'update': 'bg-blue', 'delete': 'bg-red'}[g['key']]
        else:
            # A seventh group takes the gray of "other": the palette has six validated slots.
            g['css'] = f'insights-s{i + 1}' if i < SERIES else 'insights-other'
    buckets = data['buckets']
    peak = max((b['total'] for b in buckets), default=0)
    step = max(1, round(len(buckets) / ticks))
    bars = []
    for i, b in enumerate(buckets):
        segments = [
            {'css': g['css'], 'value': v, 'label': g['label'], 'height': round(100 * v / peak, 2) if peak else 0}
            for g, v in zip(groups, b['counts']) if v
        ]
        bars.append({
            'start': b['start'], 'total': b['total'], 'change_sets': b['change_sets'],
            'segments': segments, 'partial': b['days'] < b['full_days'], 'days': b['days'],
            'tick': date_format(b['start'], 'M j') if i % step == 0 else '',
            'weekend': b['full_days'] == 1 and b['start'].weekday() >= 5,
        })
    return {'bars': bars, 'peak': peak, 'groups': groups, 'has_partial': any(b['partial'] for b in bars)}


def heatmap(grid):
    """Change sets per weekday (rows, Monday first) and hour: a step from 0 (none) to HEAT_STEPS."""
    peak = max((v for row in grid for v in row), default=0)
    rows = []
    for day, values in zip(WEEKDAYS, grid):
        cells = []
        for hour, v in enumerate(values):
            level = 0 if not v or not peak else max(1, round(HEAT_STEPS * v / peak))
            cells.append({'hour': hour, 'value': v, 'level': level})
        rows.append({'day': day, 'cells': cells, 'total': sum(values)})
    hours = [sum(row[h] for row in grid) for h in range(24)]
    return {'rows': rows, 'hours': hours, 'peak': peak, 'total': sum(hours), 'steps': range(1, HEAT_STEPS + 1)}
