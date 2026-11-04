"""Aggregates over the core changelog for the Pulse and Contributors pages."""
import contextlib
import hashlib
import statistics
from collections import defaultdict
from datetime import datetime, time, timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.core.cache import cache
from django.db import connection, transaction
from django.db.models import BigIntegerField, Count, Max, Min, Q
from django.db.models.expressions import RawSQL
from django.db.models.functions import TruncDay, TruncWeek

from core.choices import ObjectChangeActionChoices
from core.models import ObjectChange
from netbox.plugins import get_plugin_config

from . import __version__
from .filters import KIND_AUTOMATION, KIND_PERSON, NO_SCOPE, changeset_changelog_url, local_tz
from .chains import DEVICE, STAGES, build_chains
from .folding import DIFF_IGNORED_FIELDS, annotate_fold
from .labels import type_label
from .scope import SCOPES, root_scopes, scope_expression

__all__ = (
    'Insights',
)

ACTIONS = ('create', 'update', 'delete')
CACHE_VERSION = 3
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
        "ORDER BY count(DISTINCT request_id) DESC, count(*) DESC, max(id) DESC LIMIT %s) AS o",
        ('limit',),
    ),
    'recent': (
        "SELECT * FROM (SELECT 'recent', min(id), request_id::text, NULL::bigint, NULL::date, {aggs} "
        "FROM p GROUP BY request_id ORDER BY min(time) DESC LIMIT %s) AS r",
        ('limit',),
    ),
    'user_type': (
        "SELECT 'user_type', user_id, user_name, changed_object_type_id, NULL::date, {aggs} "
        "FROM p GROUP BY user_id, user_name, changed_object_type_id"
    ),
    'user_week': (
        "SELECT 'user_week', user_id, user_name, NULL::bigint, date_trunc('week', time AT TIME ZONE %s)::date, {aggs} "
        "FROM p GROUP BY 2, 3, 5",
        ('tz',),
    ),
    'week': (
        "SELECT 'week', NULL::bigint, NULL::text, NULL::bigint, date_trunc('week', time AT TIME ZONE %s)::date, {aggs} "
        "FROM p GROUP BY 5",
        ('tz',),
    ),
    'day': (
        "SELECT 'day', NULL::bigint, NULL::text, NULL::bigint, (time AT TIME ZONE %s)::date, {aggs} FROM p GROUP BY 5",
        ('tz',),
    ),
}


@contextlib.contextmanager
def no_jit():
    """
    Run the fold queries without PostgreSQL JIT. Their correlated subqueries get a high
    cost estimate, which starts JIT compilation; that costs more than the query itself
    (EXPLAIN ANALYZE, 7 days: 50 ms execution, 657 ms JIT).
    """
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute('SET LOCAL jit = off')
        yield


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
        if flt.kind:
            flt.kind_user_ids = self.kind_user_ids()

    # Actor kind (design decision 4)

    @property
    def automation_group(self):
        return get_plugin_config('netbox_changelog_insights', 'automation_group')

    def automation_user_ids(self):
        name = self.automation_group
        if not name:
            return None
        return set(get_user_model().objects.filter(groups__name=name).values_list('pk', flat=True))

    def kind_user_ids(self):
        """Ids of the users of the selected kind, for links to the core changelog."""
        ids = self.automation_user_ids()
        if ids is None or not self.flt.kind:
            return None
        if self.flt.kind == KIND_AUTOMATION:
            return sorted(ids)
        people = self.scope.filter(time__gte=self.flt.start, time__lt=self.flt.end, user_id__isnull=False)
        return sorted(set(people.values_list('user_id', flat=True).distinct()) - ids)

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
        # Site and tenant (design decision 12): an expression per record, evaluated in the database.
        for scope in SCOPES:
            value = getattr(flt, f'{scope}_id')
            if value is None:
                continue
            qs = qs.alias(**{f'scope_{scope}': scope_expression(scope)})
            if value == NO_SCOPE:
                qs = qs.filter(**{f'scope_{scope}__isnull': True})
            else:
                qs = qs.filter(**{f'scope_{scope}': value})
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
        query, params = self.primary_groups_sql(names, limit)
        keys = ('k1', 'k2', 'k3', 'k4', 'records', 'change_sets', 'create', 'update', 'delete', 'last_id',
                'first_time')
        out = {n: [] for n in names}
        with connection.cursor() as cursor:
            cursor.execute(query, params)
            for row in cursor.fetchall():
                out[row[0]].append(dict(zip(keys, row[1:])))
        return out

    def primary_groups_sql(self, names, limit=10):
        """SQL text and parameters of primary_groups(); also for EXPLAIN in tools/timing.py."""
        # The inner query is Django's own SQL with its positional parameters; the group
        # queries follow it in text order, so their parameters follow the inner ones.
        sql, params = self.primary().values(*PRIMARY_COLUMNS).order_by().query.sql_with_params()
        values = {'limit': limit, 'tz': str(self.tz)}
        params = list(params)
        parts = []
        for name in names:
            group = _GROUPS[name]
            text, needs = (group, ()) if isinstance(group, str) else group
            parts.append(f'({text.format(aggs=_AGGREGATES)})')
            params.extend(values[n] for n in needs)
        return f'WITH p AS MATERIALIZED ({sql}) ' + ' UNION ALL '.join(parts), params

    # Cache (design decision 1)

    def _cached(self, name, func):
        ttl = get_plugin_config('netbox_changelog_insights', 'cache_ttl') or 0
        if ttl <= 0:
            return func()
        # Cached values hold plain data only, and the key names both versions, so that an
        # upgrade never reads values that an older version wrote. Superusers share entries.
        params = sorted(self.flt.params().items())
        who = 'superuser' if self.user.is_superuser else self.user.pk
        versions = f'{__version__}|{getattr(settings, "VERSION", "")}'
        key = f'{name}|{versions}|{who}|{self.flt.start.isoformat()}|{self.flt.end.isoformat()}|{params}'
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
        return self._cached('pulse', self._pulse_no_jit)

    def _pulse_no_jit(self):
        with no_jit():
            return self._pulse()

    def _pulse(self):
        records = self.records()
        automation_ids = self.automation_user_ids()
        groups = self.primary_groups(('total', 'user', 'type', 'object', 'recent', 'day'))

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
            'daily': self._daily(groups['day']),
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
            r['type_label'] = type_label(types.get(r['k1']))
            r['changelog_url'] = self.flt.changelog_url(changed_object_type_id=r['k1'])
        return rows

    def _top_objects(self, rows):
        last = ObjectChange.objects.in_bulk([r['last_id'] for r in rows])
        types = ContentType.objects.in_bulk([r['k1'] for r in rows])
        for r in rows:
            rec = last.get(r['last_id'])
            r['type_label'] = type_label(types.get(r['k1']))
            r['repr'] = rec.object_repr if rec else ''
            r['deleted'] = rec is not None and rec.action == 'delete'
            r['object_url'] = None
            # The model may be gone (a disabled or removed plugin): then there is no object to link.
            model = types[r['k1']].model_class() if r['k1'] in types else None
            if rec is not None and not r['deleted'] and model is not None:
                obj = model.objects.filter(pk=rec.changed_object_id).first()
                if obj is not None and hasattr(obj, 'get_absolute_url'):
                    r['object_url'] = obj.get_absolute_url()
            r['changelog_url'] = self.flt.changelog_url(changed_object_type_id=r['k1'], changed_object_id=r['k3'])
        return rows

    def _daily(self, rows):
        """Change sets and primary changes per day in TIME_ZONE, every day of the period."""
        by_day = {r['k4']: r for r in rows}
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
            rec = first.get(r['k1'])
            out.append({
                'request_id': r['k2'],
                'time': r['first_time'],
                'primary': r['records'],
                'records': records,
                'folded': records - r['records'],
                'first': {
                    'action': rec.action,
                    'type_label': type_label(rec.changed_object_type, include_app=False),
                    'repr': rec.object_repr,
                    'user': rec.user.username if rec.user else rec.user_name,
                } if rec else None,
                'changelog_url': changeset_changelog_url(r['k2']),
            })
        return out

    # Change set

    def change_set(self, request_id):
        """All records of one change set, annotated with fold_parent, in record order."""
        qs = self.scope.filter(request_id=request_id).select_related('changed_object_type', 'user')
        with no_jit():
            return list(annotate_fold(qs, self.scope).order_by('pk'))

    # Chains (design decision 11)

    def chains(self, limit=100):
        return self._cached(f'chains{limit}', lambda: self._chains_no_jit(limit))

    def _chains_no_jit(self, limit):
        with no_jit():
            return self._chains(limit)

    def _chains(self, limit):
        flt = self.flt
        chain_gap = timedelta(hours=get_plugin_config('netbox_changelog_insights', 'chain_gap_hours', 7 * 24))
        automation_gap = timedelta(hours=get_plugin_config('netbox_changelog_insights', 'automation_gap_hours', 24))
        automation_ids = self.automation_user_ids() or set()
        # Read from chain_gap before the period: a chain may begin before it.
        records = self.scope.filter(time__gte=flt.start - chain_gap, time__lt=flt.end)
        primary_ids = set(annotate_fold(records, self.scope).filter(fold_parent__isnull=True).values_list('pk', flat=True))
        chains, resolver = build_chains(records, primary_ids, self.scope, automation_ids, chain_gap, automation_gap)

        def keep(c):
            if c.end < flt.start:
                return False
            if flt.onboarding and not c.onboarding:
                return False
            ids = {cs['user_id'] for cs in c.sets}
            names = {cs['user'] for cs in c.sets if cs['user_id'] is None}
            if flt.user_id and flt.user_id not in ids:
                return False
            if flt.user_name and flt.user_name not in names:
                return False
            if flt.kind == KIND_AUTOMATION and not ids & automation_ids:
                return False
            if flt.kind == KIND_PERSON and not (ids - automation_ids or names):
                return False
            for scope in SCOPES:
                value = getattr(flt, f'{scope}_id')
                if value is not None and root_scope[scope].get(c.root) != (None if value == NO_SCOPE else value):
                    return False
            return True

        # A chain is in the site and tenant of its root (the device or virtual machine).
        root_scope = {
            scope: root_scopes({c.root for c in chains}, scope) if getattr(flt, f'{scope}_id') is not None else {}
            for scope in SCOPES
        }
        chains = sorted((c for c in chains if keep(c)), key=lambda c: c.start, reverse=True)
        onboarding = [c for c in chains if c.onboarding]
        multi = [c for c in chains if len(c.sets) > 1]
        summary = {
            'chains': len(chains),
            'multi': len(multi),
            'onboarding': len(onboarding),
            'onboarding_median': statistics.median(c.duration.total_seconds() for c in onboarding) if onboarding else None,
            'mixed': sum(1 for c in chains if {cs['user_id'] in automation_ids for cs in c.sets} == {True, False}),
        }
        shown = chains[:limit]
        labels = self._root_labels([c.root for c in shown])
        stage_labels = dict(STAGES)
        device_type = resolver.ct_id.get(DEVICE)
        rows = []
        for c in shown:
            label = labels.get(c.root, {})
            users = []
            for cs in c.sets:
                if cs['user'] not in [u['name'] for u in users]:
                    users.append({'name': cs['user'], 'automation': cs['user_id'] in automation_ids})
            rows.append({
                'key': f'{c.root[0]}-{c.root[1]}-{int(c.start.timestamp())}',
                'root_kind': 'device' if c.root[0] == device_type else 'virtual machine',
                'root_repr': label.get('repr') or f'#{c.root[1]}',
                'root_url': label.get('url'),
                'start': c.start,
                'end': c.end,
                'duration': c.duration.total_seconds(),
                'change_sets': len(c.sets),
                'records': c.records,
                'onboarding': c.onboarding,
                'users': users,
                'stages': [{'key': s, 'label': stage_labels[s]} for s in c.stages],
                'sets': [
                    {
                        'request_id': str(cs['request_id']),
                        'time': cs['time'],
                        'user': cs['user'],
                        'automation': cs['user_id'] in automation_ids,
                        'records': len(cs['records'][c.root]),
                        'stages': [stage_labels[s] for s in sorted(cs['stages'][c.root], key=[k for k, _ in STAGES].index)],
                        'changelog_url': changeset_changelog_url(cs['request_id']),
                    }
                    for cs in c.sets[:200]
                ],
                # The object changelog of NetBox lists the root and its components.
                'changelog_url': f"{label['url']}changelog/" if label.get('url') else None,
            })
        return {'summary': summary, 'chains': rows, 'shown': len(rows)}

    def _root_labels(self, roots):
        """Latest name and, if the object exists, the URL of each root."""
        by_type = defaultdict(set)
        for t, i in roots:
            by_type[t].add(i)
        out = {}
        for t, ids in by_type.items():
            latest = self.scope.filter(changed_object_type_id=t, changed_object_id__in=ids).values(
                'changed_object_id').annotate(last=Max('pk')).order_by()
            reprs = dict(ObjectChange.objects.filter(pk__in=[r['last'] for r in latest]).values_list(
                'changed_object_id', 'object_repr'))
            model = ContentType.objects.get_for_id(t).model_class()
            objects = model.objects.in_bulk(ids) if model is not None else {}
            for i in ids:
                obj = objects.get(i)
                out[(t, i)] = {
                    'repr': str(obj) if obj is not None else reprs.get(i),
                    'url': obj.get_absolute_url() if obj is not None and hasattr(obj, 'get_absolute_url') else None,
                }
        return out

    # Scope page (design decision 12)

    def scope_page(self, scope, top=3):
        return self._cached(f'scope-{scope}-{top}', lambda: self._scope_page_no_jit(scope, top))

    def _scope_page_no_jit(self, scope, top):
        with no_jit():
            return self._scope_page(scope, top)

    def _scope_rows(self, scope):
        """
        Primary records grouped by site or tenant, in one statement: totals ("g"), change sets
        per week ("w") and primary records per object type ("t"). Key NULL: no site or tenant.
        """
        return self._primary_select(
            f"SELECT 'g', s, NULL::date, NULL::bigint, count(*), count(DISTINCT request_id), "
            f"count(*) FILTER (WHERE action = 'create'), count(*) FILTER (WHERE action = 'update'), "
            f"count(*) FILTER (WHERE action = 'delete'), "
            f"count(DISTINCT (changed_object_type_id, changed_object_id)), "
            f"count(DISTINCT coalesce(user_id::text, user_name)), max(time) FROM p GROUP BY s "
            f"UNION ALL SELECT 'w', s, date_trunc('week', time AT TIME ZONE %s)::date, NULL, "
            f"count(DISTINCT request_id), NULL, NULL, NULL, NULL, NULL, NULL, NULL FROM p GROUP BY 2, 3 "
            f"UNION ALL SELECT 't', s, NULL, changed_object_type_id, count(*), NULL, NULL, NULL, NULL, NULL, NULL, "
            f"NULL FROM p GROUP BY 2, 4 "
            f"UNION ALL SELECT 'a', NULL, NULL, NULL, count(*), count(DISTINCT request_id), NULL, NULL, NULL, NULL, "
            f"NULL, NULL FROM p",
            [str(self.tz)], scope,
        )

    def _scope_labels(self, scope, ids):
        """Name and URL of each site or tenant; a deleted one: the name of its latest record."""
        from dcim.models import Site
        from tenancy.models import Tenant
        model = {'site': Site, 'tenant': Tenant}[scope]
        objects = model.objects.in_bulk(ids)
        missing = set(ids) - set(objects)
        reprs = {}
        if missing:
            ct = ContentType.objects.get_for_model(model)
            for rec in self.scope.filter(changed_object_type=ct, changed_object_id__in=missing).order_by(
                    'changed_object_id', '-pk').distinct('changed_object_id'):
                reprs[rec.changed_object_id] = rec.object_repr
        return {
            i: {'name': str(objects[i]) if i in objects else reprs.get(i, f'#{i}'),
                'url': objects[i].get_absolute_url() if i in objects else None}
            for i in ids
        }

    def _scope_page(self, scope, top):
        """Per site or tenant: change sets, primary changes by action, objects, users, weekly trend, top types."""
        result = self._scope_rows(scope)
        weeks = []
        w = self.flt.start_date - timedelta(days=self.flt.start_date.weekday())
        while w <= self.flt.end_date:
            weeks.append(w)
            w += timedelta(days=7)
        weekly, types = defaultdict(dict), defaultdict(list)
        total = {'records': 0, 'change_sets': 0}
        groups = []
        for tag, key, week, type_id, n, sets, create, update, delete, objects, users, last in result:
            if tag == 'g':
                groups.append({
                    'key': key, 'records': n, 'change_sets': sets, 'create': create, 'update': update,
                    'delete': delete, 'objects': objects, 'users': users, 'last': last,
                })
            elif tag == 'w':
                weekly[key][week] = n
            elif tag == 't':
                types[key].append((type_id, n))
            else:
                total = {'records': n, 'change_sets': sets}
        labels = self._scope_labels(scope, [g['key'] for g in groups if g['key'] is not None])
        ct = ContentType.objects.in_bulk({t for v in types.values() for t, _ in v})
        for g in groups:
            g.update(labels.get(g['key'], {'name': None, 'url': None}))
            g['weekly'] = [weekly[g['key']].get(w, 0) for w in weeks]
            g['top_types'] = [
                {'type_id': t, 'type_label': type_label(ct.get(t), include_app=False), 'records': n}
                for t, n in sorted(types[g['key']], key=lambda tn: (-tn[1], tn[0]))[:top]
            ]
        # Most change sets first; the records without a site or tenant last.
        groups.sort(key=lambda g: (g['key'] is None, -g['change_sets'], -g['records'], str(g['name'])))
        return {'groups': groups, 'weeks': weeks, 'total': total}

    # Activity page

    def _primary_select(self, select, extra_params=(), scope=None):
        """
        Run select over the primary records "p" of the filter (one CTE, the fold evaluated
        once). With scope ("site" or "tenant"), "p" has a column "s": its id, or NULL.
        """
        # The scope expression is a column of the primary query itself: PostgreSQL evaluates
        # it once per primary record. A join back to core_objectchange by id gave a plan that
        # scanned the table once per record when the filter made the row estimate too low.
        qs = self.primary()
        if scope:
            qs = qs.annotate(s=scope_expression(scope))
        else:
            qs = qs.annotate(s=RawSQL('NULL::bigint', (), output_field=BigIntegerField()))
        sql, params = qs.values(*PRIMARY_COLUMNS, 's').order_by().query.sql_with_params()
        with connection.cursor() as cursor:
            cursor.execute(f'WITH p AS MATERIALIZED ({sql}) {select}', list(params) + list(extra_params))
            return cursor.fetchall()

    def activity_page(self, by, bucket, top=6):
        return self._cached(f'activity-page-{by}-{bucket}-{top}', lambda: self._activity_page_no_jit(by, bucket, top))

    def _activity_page_no_jit(self, by, bucket, top):
        with no_jit():
            return self._activity_page(by, bucket, top)

    def _activity_page(self, by, bucket, top):
        """
        Primary changes per bucket (day or week) split by one dimension (user, app, model,
        action, site, tenant), the top groups and "other"; change sets per bucket; and change sets per
        weekday and hour, all in TIME_ZONE.
        """
        tz = str(self.tz)
        # One statement, so that the fold is evaluated once: series rows ("r"), change sets
        # per bucket ("s") and change sets per weekday and hour ("h").
        trunc = "date_trunc(%s, time AT TIME ZONE %s)::date"
        local = "time AT TIME ZONE %s"
        scope = by if by in SCOPES else None
        result = self._primary_select(
            f"SELECT 'r', {trunc}, user_id, user_name, changed_object_type_id, action, s, count(*) FROM p "
            "GROUP BY 2, 3, 4, 5, 6, 7 "
            f"UNION ALL SELECT 's', {trunc}, NULL, NULL, NULL, NULL, NULL, count(DISTINCT request_id) FROM p "
            "GROUP BY 2 "
            f"UNION ALL SELECT 'h', NULL, extract(isodow FROM {local})::bigint, NULL, "
            f"extract(hour FROM {local})::bigint, NULL, NULL, count(DISTINCT request_id) FROM p GROUP BY 3, 5",
            (bucket, tz, bucket, tz, tz, tz),
            scope,
        )
        # r: (bucket, user id, user name, type id, action, site or tenant id, primary changes)
        rows = [r[1:] for r in result if r[0] == 'r']
        sets = {r[1]: r[7] for r in result if r[0] == 's'}
        heat = [(r[2], r[4], r[7]) for r in result if r[0] == 'h']

        types = ContentType.objects.in_bulk({r[3] for r in rows})
        action_labels = {c[0]: str(c[1]) for c in ObjectChangeActionChoices.CHOICES}
        users = dict(get_user_model().objects.filter(
            pk__in={r[1] for r in rows if r[1]}).values_list('pk', 'username'))
        scope_labels = self._scope_labels(scope, {r[5] for r in rows if r[5] is not None}) if scope else {}
        none_label = {'site': 'No site', 'tenant': 'No tenant'}

        def key(r):
            """(group key, label, filter parameters for links) of a row."""
            if scope:
                if r[5] is None:
                    return 'none', none_label[scope], {f'{scope}_id': 'null'}
                return r[5], scope_labels[r[5]]['name'], {f'{scope}_id': r[5]}
            if by == 'user':
                if r[1]:
                    return ('u', r[1]), users.get(r[1], r[2]), {'user_id': r[1]}
                return ('n', r[2]), r[2], {'user_name': r[2]}
            if by == 'action':
                return r[4], action_labels.get(r[4], r[4]), {'action': r[4]}
            ct = types.get(r[3])
            if by == 'app':
                app = ct.app_label if ct else ''
                label = type_label(ct).split(' > ')[0] if ct else ''
                return app, label, {}
            return r[3], type_label(ct), {'type_id': r[3]}

        totals, labels, params, series = defaultdict(int), {}, {}, defaultdict(lambda: defaultdict(int))
        for r in rows:
            k, label, p = key(r)
            totals[k] += r[6]
            labels[k], params[k] = label, p
            series[r[0]][k] += r[6]
        ranked = sorted(totals, key=lambda k: (-totals[k], str(labels[k])))
        if by == 'action':
            ranked = [a for a in ACTIONS if a in totals]
        # Fold into "other" only two or more groups: one group is shown under its own name.
        shown, other = (ranked, []) if len(ranked) <= top + 1 else (ranked[:top], ranked[top:])
        groups = [{'key': str(k), 'label': labels[k], 'total': totals[k], 'params': params[k]} for k in shown]
        if other:
            groups.append({'key': 'other', 'label': None, 'total': sum(totals[k] for k in other), 'params': None,
                           'count': len(other)})

        # Every bucket of the period, in order; weeks start on Monday.
        step = timedelta(days=1 if bucket == 'day' else 7)
        d = self.flt.start_date
        if bucket == 'week':
            d -= timedelta(days=d.weekday())
        buckets = []
        while d <= self.flt.end_date:
            values = series.get(d, {})
            counts = [values.get(k, 0) for k in shown]
            if other:
                counts.append(sum(values.get(k, 0) for k in other))
            first, last = max(d, self.flt.start_date), min(d + step - timedelta(days=1), self.flt.end_date)
            buckets.append({
                'start': d, 'counts': counts, 'total': sum(counts), 'change_sets': sets.get(d, 0),
                'days': (last - first).days + 1, 'full_days': step.days,
            })
            d += step

        grid = [[0] * 24 for _ in range(7)]
        for dow, hour, n in heat:
            grid[dow - 1][hour] = n
        return {'groups': groups, 'buckets': buckets, 'heat': grid}

    # Hotspots (design decisions 5 and 6)

    def _changed_fields_sql(self):
        """SQL and parameters: one row per primary update and field it changed (as ObjectChange.diff())."""
        sql, params = self.primary().filter(action='update').values(
            'id', 'time', 'request_id', 'user_id', 'user_name', 'changed_object_type_id', 'changed_object_id',
            'prechange_data', 'postchange_data',
        ).order_by().query.sql_with_params()
        ignored = ', '.join(f"'{f}'" for f in DIFF_IGNORED_FIELDS)
        pre, post = "COALESCE(u.prechange_data, '{}'::jsonb)", "COALESCE(u.postchange_data, '{}'::jsonb)"
        fields = (
            f"SELECT u.*, k.key AS field FROM ({sql}) AS u, jsonb_object_keys({pre} || {post}) AS k(key) "
            f"WHERE k.key NOT IN ({ignored}) AND left(k.key, 1) <> '_' "
            f"AND ({pre} -> k.key) IS DISTINCT FROM ({post} -> k.key)"
        )
        return fields, list(params)

    def hotspots(self, limit=25):
        return self._cached(f'hotspots{limit}', lambda: self._hotspots_no_jit(limit))

    def _hotspots_no_jit(self, limit):
        with no_jit():
            return self._hotspots(limit)

    def _hotspots(self, limit):
        """Most changed objects (by change sets) and most changed fields (by primary updates)."""
        sql, params = self.primary().values(*PRIMARY_COLUMNS).order_by().query.sql_with_params()
        with connection.cursor() as cursor:
            cursor.execute(
                f"SELECT changed_object_type_id, changed_object_id, count(DISTINCT request_id), count(*), "
                f"count(*) FILTER (WHERE action = 'update'), count(DISTINCT coalesce(user_id::text, user_name)), "
                f"max(id), min(time), max(time) "
                f"FROM ({sql}) AS p GROUP BY 1, 2 ORDER BY 3 DESC, 4 DESC, 7 DESC LIMIT %s",
                list(params) + [limit],
            )
            objects = cursor.fetchall()
            fields_sql, fields_params = self._changed_fields_sql()
            cursor.execute(
                f"SELECT changed_object_type_id, field, count(*), count(DISTINCT changed_object_id), "
                f"count(DISTINCT coalesce(user_id::text, user_name)), max(time) "
                f"FROM ({fields_sql}) AS f GROUP BY 1, 2 ORDER BY 3 DESC, 4 DESC, 2 LIMIT %s",
                fields_params + [limit],
            )
            fields = cursor.fetchall()

        types = ContentType.objects.in_bulk({r[0] for r in objects} | {r[0] for r in fields})
        last = ObjectChange.objects.in_bulk([r[6] for r in objects])
        object_rows = []
        for t, oid, sets, changes, updates, users, last_id, first, latest in objects:
            ct, rec = types.get(t), last.get(last_id)
            model = ct.model_class() if ct else None
            url, parent = None, None
            deleted = rec is not None and rec.action == 'delete'
            if model is not None and not deleted:
                obj = model.objects.filter(pk=oid).first()
                if obj is not None and hasattr(obj, 'get_absolute_url'):
                    url = obj.get_absolute_url()
                # A component (interface, port) is named within its device or virtual machine.
                host = getattr(obj, 'device', None) or getattr(obj, 'virtual_machine', None) if obj else None
                parent = str(host) if host is not None else None
            object_rows.append({
                'type_id': t, 'type_label': type_label(ct), 'object_id': oid, 'repr': rec.object_repr if rec else f'#{oid}',
                'deleted': deleted, 'object_url': url, 'parent': parent, 'change_sets': sets, 'changes': changes, 'updates': updates,
                'users': users, 'first': first, 'last': latest,
                'changelog_url': self.flt.changelog_url(changed_object_type_id=t, changed_object_id=oid),
            })
        field_rows = [
            {'type_id': t, 'type_label': type_label(types.get(t)), 'field': field, 'updates': n, 'objects': objs,
             'users': users, 'last': latest}
            for t, field, n, objs, users, latest in fields
        ]
        return {'objects': object_rows, 'fields': field_rows}

    def field_changes(self, type_id, field, limit=100):
        """Latest primary updates of one object type that changed field, with the values before and after."""
        fields_sql, params = self._changed_fields_sql()
        with no_jit(), connection.cursor() as cursor:
            cursor.execute(
                f"SELECT id, time, user_id, user_name, changed_object_id, "
                f"prechange_data -> field, postchange_data -> field, count(*) OVER () "
                f"FROM ({fields_sql}) AS f WHERE changed_object_type_id = %s AND field = %s "
                f"ORDER BY time DESC, id DESC LIMIT %s",
                params + [type_id, field, limit],
            )
            rows = cursor.fetchall()
        records = ObjectChange.objects.prefetch_related('related_object').in_bulk([r[0] for r in rows])
        users = dict(get_user_model().objects.filter(pk__in={r[2] for r in rows if r[2]}).values_list('pk', 'username'))
        out = []
        for rid, t, uid, uname, oid, before, after, total in rows:
            rec = records.get(rid)
            out.append({
                'id': rid, 'time': t, 'user': users.get(uid, uname), 'object_id': oid,
                'repr': rec.object_repr if rec else f'#{oid}', 'before': before, 'after': after,
                # The related object (the device of an interface) names a component.
                'parent': str(rec.related_object) if rec and rec.related_object else None,
                'url': rec.get_absolute_url() if rec else None,
            })
        return {'rows': out, 'total': rows[0][7] if rows else 0}

    # API for Grafana

    def activity(self, group, limit=10):
        """Flat rows for the REST API: group is 'day', 'week', 'user', 'type', 'site' or 'tenant'."""
        return self._cached(f'activity-{group}-{limit}', lambda: self._activity_no_jit(group, limit))

    def _activity_no_jit(self, group, limit):
        with no_jit():
            return self._activity(group, limit)

    def _activity(self, group, limit):
        def counts(r):
            return {
                'change_sets': r['change_sets'], 'changes': r['records'],
                'created': r['create'], 'updated': r['update'], 'deleted': r['delete'],
            }

        if group in ('day', 'week'):
            rows = {r['k4']: r for r in self.primary_groups((group,))[group]}
            trunc = TruncDay if group == 'day' else TruncWeek
            records = dict(self.records().annotate(b=trunc('time', tzinfo=self.tz)).order_by().values('b').annotate(
                n=Count('pk')).values_list('b', 'n'))
            records = {k.astimezone(self.tz).date(): n for k, n in records.items()}
            step = timedelta(days=1 if group == 'day' else 7)
            d = self.flt.start_date
            if group == 'week':
                d -= timedelta(days=d.weekday())
            out = []
            zero = {'change_sets': 0, 'records': 0, 'create': 0, 'update': 0, 'delete': 0}
            while d <= self.flt.end_date:
                out.append({
                    'time': datetime.combine(d, time.min, self.tz).isoformat(),
                    **counts(rows.get(d, zero)),
                    'records': records.get(d, 0),
                })
                d += step
            return out
        if group == 'user':
            groups = self.primary_groups(('user',))
            users = self._users(groups['user'], self.records(), self.automation_user_ids())[:limit]
            return [
                {'user': u['user_name'], 'kind': u['kind'], 'user_deleted': u['deleted'], **counts(u | {
                    'records': u['create'] + u['update'] + u['delete']}), 'records': u['records']}
                for u in users
            ]
        if group == 'type':
            rows = self._top_types(self.primary_groups(('type',))['type'], limit)
            return [{'type': r['type_label'], **counts(r)} for r in rows]
        if group in SCOPES:
            # Records without a site or tenant: one row with the name null, in its place by count.
            rows = sorted(self._scope_page(group, 0)['groups'], key=lambda g: (-g['change_sets'], -g['records']))
            return [{group: g['name'], f'{group}_id': g['key'], **counts(g)} for g in rows[:limit]]
        raise ValueError(group)

    # User page

    def user_span(self):
        """First and last change of the filtered user over the whole changelog in scope; kind."""
        qs = self.scope
        if self.flt.user_id:
            qs = qs.filter(user_id=self.flt.user_id)
        else:
            qs = qs.filter(user__isnull=True, user_name=self.flt.user_name)
        span = qs.aggregate(first=Min('time'), last=Max('time'))
        span['kind'] = self.kind_of(self.flt.user_id, self.automation_user_ids())
        return span

    # Contributors

    def contributors(self):
        return self._cached('contributors', self._contributors_no_jit)

    def _contributors_no_jit(self):
        with no_jit():
            return self._contributors()

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
                    'type_label': type_label(ct.get(t), include_app=False), 'records': n,
                    'changelog_url': self._user_changelog_url(u, changed_object_type_id=t),
                }
                for t, n in top
            ]
            u['action_urls'] = {a: self._user_changelog_url(u, action=a) for a in ACTIONS}
        return {'users': users, 'weeks': weeks}
