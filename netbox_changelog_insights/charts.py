"""Chart data for templates. Bars are HTML; sparklines are inline SVG. No chart library."""
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


def sparkline(values, peak, weeks, width=96, height=24):
    """SVG polyline points for a weekly series, scaled to a shared peak."""
    if not values:
        return None
    n = len(values)
    dx = width / max(n - 1, 1)
    pad = 2
    points = []
    for i, v in enumerate(values):
        y = height - pad - ((height - 2 * pad) * v / peak if peak else 0)
        points.append(f'{i * dx:.1f},{y:.1f}')
    title = ', '.join(f"{date_format(w, 'M j')}: {v}" for w, v in zip(weeks, values))
    return {'points': ' '.join(points), 'width': width, 'height': height, 'title': title}
