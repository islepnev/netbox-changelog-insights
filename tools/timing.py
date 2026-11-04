"""
Time the plugin pages (Pulse, Contributors, Activity, Chains) on a NetBox database. Read-only: the database session is set to
read-only before the first query, the cache is off, and the script prints timings,
counts and query plans only, with no names or values from the changelog.

Run in the NetBox directory that holds manage.py, with the NetBox Python and the
plugin installed and in PLUGINS:

    cd /opt/netbox/netbox
    /opt/netbox/venv/bin/python /path/to/tools/timing.py > timing.txt

Options: --repeat N (runs per case, default 2), --no-views (skip the full page renders),
--no-explain (skip EXPLAIN ANALYZE).
"""
import argparse
import os
import sys
import time
import traceback

sys.path.insert(0, os.environ.get('NETBOX_ROOT', os.getcwd()))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'netbox.settings')

import django  # noqa: E402

django.setup()

from django.conf import settings  # noqa: E402
from django.contrib.auth import get_user_model  # noqa: E402
from django.db import connection  # noqa: E402
from django.test import RequestFactory  # noqa: E402

from core.models import ObjectChange  # noqa: E402

from netbox_changelog_insights import __version__  # noqa: E402
from netbox_changelog_insights.filters import InsightsFilter  # noqa: E402
from netbox_changelog_insights.insights import Insights, no_jit  # noqa: E402
from netbox_changelog_insights.views import ActivityView, ChainsView, ContributorsView, PulseView  # noqa: E402

PLUGIN = 'netbox_changelog_insights'


def setting(cursor, name):
    cursor.execute(f'SHOW {name}')
    return cursor.fetchone()[0]


def timed(func, repeat):
    out = []
    for _ in range(repeat):
        t = time.perf_counter()
        func()
        out.append(time.perf_counter() - t)
    return ' '.join(f'{s:7.2f}' for s in out)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--repeat', type=int, default=2)
    parser.add_argument('--no-views', action='store_true')
    parser.add_argument('--no-explain', action='store_true')
    args = parser.parse_args()

    # Cache off, in this process only.
    settings.PLUGINS_CONFIG.setdefault(PLUGIN, {})['cache_ttl'] = 0

    with connection.cursor() as cursor:
        cursor.execute('SET default_transaction_read_only = on')
        cursor.execute('SELECT version()')
        pg = cursor.fetchone()[0].split(',')[0]
        cursor.execute("SELECT pg_size_pretty(pg_total_relation_size('core_objectchange'))")
        size = cursor.fetchone()[0]
        print(f'NetBox {settings.VERSION}, plugin {__version__}, {pg}')
        for name in ('jit', 'jit_above_cost', 'work_mem', 'shared_buffers', 'effective_cache_size',
                     'random_page_cost', 'max_parallel_workers_per_gather', 'default_transaction_read_only'):
            print(f'{name} = {setting(cursor, name)}')
    print(f'core_objectchange: {ObjectChange.objects.count()} records, {size}')
    print(f'automation_group set: {bool(settings.PLUGINS_CONFIG[PLUGIN].get("automation_group"))}')
    oldest = ObjectChange.objects.order_by('time').values_list('time', flat=True).first()
    if oldest is None:
        print('No records.')
        return
    print(f'oldest record: {oldest.date()}')

    user = get_user_model().objects.filter(is_superuser=True, is_active=True).order_by('pk').first()
    if user is None:
        print('No active superuser: the script needs one to read the full changelog.')
        return
    factory = RequestFactory()

    def request(path, params):
        r = factory.get(path, params)
        r.user = user
        r.htmx = None
        return r

    cases = [('7 days', {'period': 7}), ('30 days', {'period': 30}), ('90 days', {'period': 90}),
             ('full', {'period': 'custom', 'start': oldest.date().isoformat()})]
    pages = (('pulse', lambda i: i.pulse(), PulseView), ('contributors', lambda i: i.contributors(), ContributorsView),
             ('activity', lambda i: i.activity_page('user', 'day' if i.flt.days <= 92 else 'week'), ActivityView),
             ('chains', lambda i: i.chains(), ChainsView))

    print()
    print(f'Seconds per run ({args.repeat} runs). "data": aggregates only; "page": view with template.')
    print(f'{"period":8} {"records":>8} {"page":13} {"kind":5} runs')
    for label, params in cases:
        flt = InsightsFilter.from_request(request('/', params))
        n = ObjectChange.objects.filter(time__gte=flt.start, time__lt=flt.end).count()
        for name, method, view in pages:
            insights = Insights(user, flt)
            try:
                print(f'{label:8} {n:8} {name:13} data  {timed(lambda: method(insights), args.repeat)}', flush=True)
                if not args.no_views:
                    runs = timed(lambda: view.as_view()(request('/', params)), args.repeat)
                    print(f'{label:8} {n:8} {name:13} page  {runs}', flush=True)
            except Exception:
                print(f'{label:8} {n:8} {name:13} error')
                traceback.print_exc(limit=3)
                connection.close()

    if args.no_explain:
        return
    flt = InsightsFilter.from_request(request('/', {'period': 90}))
    query, params = Insights(user, flt).primary_groups_sql(('total', 'user', 'type', 'object', 'recent', 'day'))
    for jit in (False, True):
        print()
        print(f'EXPLAIN ANALYZE, Pulse fold query, 90 days, JIT {"on (server setting)" if jit else "off (plugin)"}')
        if jit:
            with connection.cursor() as cursor:
                cursor.execute('EXPLAIN (ANALYZE, BUFFERS) ' + query, params)
                rows = cursor.fetchall()
        else:
            with no_jit(), connection.cursor() as cursor:
                cursor.execute('EXPLAIN (ANALYZE, BUFFERS) ' + query, params)
                rows = cursor.fetchall()
        for (line,) in rows:
            print(line)


if __name__ == '__main__':
    main()
