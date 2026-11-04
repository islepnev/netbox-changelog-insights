"""Aggregates over the core changelog for the Pulse and Contributors pages."""
import hashlib
from collections import defaultdict
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.core.cache import cache
from django.db import connection
from django.db.models import Count, Max, Min, Q
from django.db.models.functions import TruncDate

from core.models import ObjectChange
from netbox.plugins import get_plugin_config

from .filters import KIND_AUTOMATION, KIND_PERSON, changeset_changelog_url, local_tz
from .folding import annotate_fold

__all__ = (
    'Insights',
)

ACTIONS = ('create', 'update', 'delete')
CACHE_VERSION = 2
PRIMARY_COLUMNS = (
    'id', 'request_id', 'time', 'user_id', 'user_name', 'action', 'changed_object_type_id', 'changed_object_id',
)
_AGGREGATES = (
    "count(*), count(DISTINCT request_id), "
    "count(*) FILTER (WHERE action = 'create'), "
    "count(*) FILTER (WHERE action = 'update'), "
    "count(*) FILTER (WHERE action = 'delete'), "
    "max(id), min(time)"
)
# One SELECT per group over the primary records "p". Key columns: k1 bigint, k2 text, k3 bigint, k4 date.
_GROUPS = {
    'total': (
        "SELECT 'total', NULL::bigint, NULL::text, count(DISTINCT (changed_object_type_id, changed_object_id)), "
        "NULL::date, {aggs} FROM p"
    ),
    'user': "SELECT 'user', user_id, user_name, NULL::bigint, NULL::date, {aggs} FROM p GROUP BY user_id, user_name",
    'type': "SELECT 'type', changed_object_type_id, NULL::text, NULL::bigint, NULL::date, {aggs} FROM p GROUP BY changed_object_type_id",
    'object': (
        "SELECT * FROM (SELECT 'object', changed_object_type_id, NULL::text, changed_object_id, NULL::date, {aggs} "
        "FROM p GROUP BY changed_object_type_id, changed_object_id "
        "ORDER BY count(DISTINCT request_id) DESC, count(*) DESC, max(id) DESC LIMIT %(limit)s) AS o"
    ),
    'recent': (
        "SELECT * FROM (SELECT 'recent', min(id), request_id::text, NULL::bigint, NULL::date, {aggs} "
        "FROM p GROUP BY request_id ORDER BY min(time) DESC LIMIT %(limit)s) AS r"
    ),
    'user_type': (
        "SELECT 'user_type', user_id, user_name, changed_object_type_id, NULL::date, {aggs} "
        "FROM p GROUP BY user_id, user_name, changed_object_type_id"
    ),
    'user_week': (
        "SELECT 'user_week', user_id, user_name, NULL::bigint, date_trunc('week', time AT TIME ZONE %(tz)s)::date, {aggs} "
        "FROM p GROUP BY user_id, user_name, date_trunc('week', time AT TIME ZONE %(tz)s)::date"
    ),
}


def _action_counts():
    return {a: Count('pk', filter=Q(action=a)) for a in ACTIONS}


def _user_key(row):
    return ('id', row['user_id']) if row['user_id'] else ('name', row['user_name'])


class Insights:
    """
    Aggregates for one user (permissions) and one filter. The scope is the changelog
    the user may view (design decision 7); all numbers are computed inside it.
    """

    def __init__(self, user, flt):
        self.user = user
        self.flt = flt
        self.tz = local_tz()
        self.scope = ObjectChange.objects.restrict(user, 'view')

    # Actor kind (design decision 4)

    @property
    def automation_group(self):
        return get_plugin_config('netbox_changelog_insights', 'automation_group')

    def automation_user_ids(self):
        name = self.automation_group
        if not name:
            return None
        return set(get_user_model().objects.filter(groups__name=name).values_list('pk', flat=True))

    @staticmethod
    def kind_of(user_id, automation_ids):
        if automation_ids is None:
            return None
        return KIND_AUTOMATION if user_id in automation_ids else KIND_PERSON

    # Querysets

    def records(self):
        """Records in the period that match the filter, folded or not."""
        flt = self.flt
        qs = self.scope.filter(time__gte=flt.start, time__lt=flt.end)
        if flt.user_id:
            qs = qs.filter(user_id=flt.user_id)
        elif flt.user_name:
            qs = qs.filter(user__isnull=True, user_name=flt.user_name)
        if flt.type_id:
            qs = qs.filter(changed_object_type_id=flt.type_id)
        if flt.action:
            qs = qs.filter(action=flt.action)
        if flt.kind:
            ids = self.automation_user_ids()
            if ids is not None:
                if flt.kind == KIND_AUTOMATION:
                    qs = qs.filter(user_id__in=ids)
                else:
                    qs = qs.exclude(user_id__in=ids)
        return qs

    def primary(self):
        """Primary records: records that do not fold into another record of their change set."""
        return annotate_fold(self.records(), self.scope).filter(fold_parent__isnull=True)

    def primary_groups(self, names, limit=10):
        """
        Aggregates of the primary records, grouped several ways in one query, so that the
        fold is evaluated once. Return {group: [row, ...]}; each row has the keys
        k1, k2, k3, k4, records, change_sets, create, update, delete, last_id, first_time.
        """
        sql, params = self.primary().values(*PRIMARY_COLUMNS).order_by().query.sql_with_params()
        # The outer query uses named parameters: escape literal "%" and rename the inner "%s".
        sql = sql.replace('%', '%%').replace('%%s', '%s')
        parts = [_GROUPS[n].format(aggs=_AGGREGATES) for n in names]
        named = {'limit': limit, 'tz': str(self.tz)}
        # Mixed parameter styles are not allowed: convert the inner positional ones to named.
        inner = {}
        for i, value in enumerate(params):
            inner[f'p{i}'] = value
            sql = sql.replace('%s', f'%(p{i})s', 1)
        query = f'WITH p AS MATERIALIZED ({sql}) ' + ' UNION ALL '.join(f'({part})' for part in parts)
        keys = ('k1', 'k2', 'k3', 'k4', 'records', 'change_sets', 'create', 'update', 'delete', 'last_id',
                'first_time')
        out = {n: [] for n in names}
        with connection.cursor() as cursor:
            cursor.execute(query, {**inner, **named})
            for row in cursor.fetchall():
                out[row[0]].append(dict(zip(keys, row[1:])))
        return out

    # Cache (design decision 1)

    def _cached(self, name, func):
        ttl = get_plugin_config('netbox_changelog_insights', 'cache_ttl') or 0
        if ttl <= 0:
            return func()
        params = sorted(self.flt.params().items())
        key = f'{name}|{self.user.pk}|{self.flt.start.isoformat()}|{self.flt.end.isoformat()}|{params}'
        key = f'changelog_insights:{CACHE_VERSION}:' + hashlib.sha256(key.encode()).hexdigest()
        value = cache.get(key)
        if value is None:
            value = func()
            cache.set(key, value, ttl)
        return value

    # Pulse

    def oldest_record(self):
        return self.scope.aggregate(t=Min('time'))['t']

    def pulse(self):
        return self._cached('pulse', self._pulse)

    def _pulse(self):
        records = self.records()
        automation_ids = self.automation_user_ids()
        groups = self.primary_groups(('total', 'user', 'type', 'object', 'recent'))

        totals = records.aggregate(
            records=Count('pk'),
            change_sets=Count('request_id', distinct=True),
            **_action_counts(),
        )
        total = groups['total'][0]
        prim = {k: total[k] for k in ('records', 'change_sets', 'create', 'update', 'delete')}
        prim['objects'] = total['k3']
        totals['folded'] = totals['records'] - prim['records']

        users = self._users(groups['user'], records, automation_ids)
        kinds = None
        if automation_ids is not None:
            kinds = {KIND_PERSON: 0, KIND_AUTOMATION: 0}
            for u in users:
                kinds[u['kind']] += 1

        return {
            'totals': totals,
            'primary': prim,
            'active_users': len(users),
            'kinds': kinds,
            'top_users': users[:10],
            'top_types': self._top_types(groups['type']),
            'top_objects': self._top_objects(groups['object']),
            'daily': self._daily(records),
            'recent': self._recent(groups['recent']),
        }

    def _users(self, rows, records, automation_ids):
        """Users with change sets in the period, most active first."""
        rec_by_key = defaultdict(int)
        for row in records.values('user_id', 'user_name').annotate(records=Count('pk')).order_by():
            rec_by_key[_user_key(row)] += row['records']

        # A user renamed during the period appears under several names: merge by id.
        users = {}
        for row in rows:
            key = _user_key({'user_id': row['k1'], 'user_name': row['k2']})
            u = users.setdefault(key, {
                'user_id': row['k1'], 'user_name': row['k2'],
                'change_sets': 0, 'create': 0, 'update': 0, 'delete': 0,
            })
            for k in ('change_sets', 'create', 'update', 'delete'):
                u[k] += row[k]
        names = dict(get_user_model().objects.filter(
            pk__in=[u['user_id'] for u in users.values() if u['user_id']]
        ).values_list('pk', 'username'))
        for key, u in users.items():
            if u['user_id'] in names:
                u['user_name'] = names[u['user_id']]
            u['deleted'] = u['user_id'] is None
            u['records'] = rec_by_key.get(key, 0)
            u['kind'] = self.kind_of(u['user_id'], automation_ids)
            u['changelog_url'] = self._user_changelog_url(u)
        return sorted(users.values(), key=lambda u: (-u['change_sets'], u['user_name']))

    def _user_changelog_url(self, u, **extra):
        if u['user_id']:
            return self.flt.changelog_url(user_id=u['user_id'], **extra)
        return self.flt.changelog_url(user_name=u['user_name'], **extra)

    def _top_types(self, rows, limit=10):
        rows = sorted(rows, key=lambda r: (-r['change_sets'], -r['records']))[:limit]
        types = ContentType.objects.in_bulk([r['k1'] for r in rows])
        for r in rows:
            r['changed_object_type_id'] = r['k1']
            r['type'] = types.get(r['k1'])
            r['changelog_url'] = self.flt.changelog_url(changed_object_type_id=r['k1'])
        return rows

    def _top_objects(self, rows):
        last = ObjectChange.objects.in_bulk([r['last_id'] for r in rows])
        types = ContentType.objects.in_bulk([r['k1'] for r in rows])
        for r in rows:
            rec = last.get(r['last_id'])
            r['type'] = types.get(r['k1'])
            r['repr'] = rec.object_repr if rec else ''
            r['deleted'] = rec is not None and rec.action == 'delete'
            r['object_url'] = None
            if rec is not None and not r['deleted']:
                obj = rec.changed_object
                if obj is not None and hasattr(obj, 'get_absolute_url'):
                    r['object_url'] = obj.get_absolute_url()
            r['changelog_url'] = self.flt.changelog_url(changed_object_type_id=r['k1'], changed_object_id=r['k3'])
        return rows

    def _daily(self, records):
        """Change sets per day in TIME_ZONE, every day of the period."""
        rows = records.annotate(day=TruncDate('time', tzinfo=self.tz)).values('day').annotate(
            change_sets=Count('request_id', distinct=True),
            records=Count('pk'),
        ).order_by('day')
        by_day = {r['day']: r for r in rows}
        days = []
        d = self.flt.start_date
        while d <= self.flt.end_date:
            r = by_day.get(d, {'change_sets': 0, 'records': 0})
            days.append({'day': d, 'change_sets': r['change_sets'], 'records': r['records']})
            d += timedelta(days=1)
        return days

    def _recent(self, rows):
        """Latest change sets: first primary record, counts of primary and folded records."""
        ids = [r['k2'] for r in rows]
        totals = {str(k): n for k, n in self.scope.filter(request_id__in=ids).values('request_id').annotate(
            n=Count('pk')
        ).order_by().values_list('request_id', 'n')}
        first = ObjectChange.objects.select_related('changed_object_type', 'user').in_bulk([r['k1'] for r in rows])
        out = []
        for r in sorted(rows, key=lambda r: r['first_time'], reverse=True):
            records = totals.get(r['k2'], 0)
            out.append({
                'request_id': r['k2'],
                'time': r['first_time'],
                'primary': r['records'],
                'records': records,
                'folded': records - r['records'],
                'first': first.get(r['k1']),
                'changelog_url': changeset_changelog_url(r['k2']),
            })
        return out

    # Change set

    def change_set(self, request_id):
        """All records of one change set, annotated with fold_parent, in record order."""
        qs = self.scope.filter(request_id=request_id).select_related('changed_object_type', 'user')
        return list(annotate_fold(qs, self.scope).order_by('pk'))

    # Contributors

    def contributors(self):
        return self._cached('contributors', self._contributors)

    def _contributors(self):
        records = self.records()
        automation_ids = self.automation_user_ids()
        groups = self.primary_groups(('user', 'user_type', 'user_week'))
        users = self._users(groups['user'], records, automation_ids)
        keys = {_user_key(u) for u in users}

        # First and last change over the whole changelog in scope.
        span = {}
        for row in self.scope.values('user_id', 'user_name').annotate(
            first=Min('time'), last=Max('time'),
        ).order_by():
            key = _user_key(row)
            if key in keys:
                s = span.setdefault(key, {'first': row['first'], 'last': row['last']})
                s['first'] = min(s['first'], row['first'])
                s['last'] = max(s['last'], row['last'])

        # Change sets per week; weeks start on Monday in TIME_ZONE.
        weeks = []
        w = self.flt.start_date - timedelta(days=self.flt.start_date.weekday())
        while w <= self.flt.end_date:
            weeks.append(w)
            w += timedelta(days=7)
        weekly = defaultdict(lambda: defaultdict(int))
        for row in groups['user_week']:
            weekly[_user_key({'user_id': row['k1'], 'user_name': row['k2']})][row['k4']] += row['change_sets']

        # Top types per user, by primary records.
        types = defaultdict(lambda: defaultdict(int))
        for row in groups['user_type']:
            types[_user_key({'user_id': row['k1'], 'user_name': row['k2']})][row['k3']] += row['records']
        ct = ContentType.objects.in_bulk({t for v in types.values() for t in v})

        for u in users:
            key = _user_key(u)
            u.update(span.get(key, {'first': None, 'last': None}))
            u['weekly'] = [weekly[key].get(w, 0) for w in weeks]
            top = sorted(types[key].items(), key=lambda kv: -kv[1])[:3]
            u['top_types'] = [
                {
                    'type': ct.get(t), 'records': n,
                    'changelog_url': self._user_changelog_url(u, changed_object_type_id=t),
                }
                for t, n in top
            ]
            u['action_urls'] = {a: self._user_changelog_url(u, action=a) for a in ACTIONS}
        return {'users': users, 'weeks': weeks}
