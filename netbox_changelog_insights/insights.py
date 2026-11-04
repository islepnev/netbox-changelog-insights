"""Aggregates over the core changelog for the Pulse and Contributors pages."""
import hashlib
from collections import defaultdict
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.core.cache import cache
from django.db.models import Count, Max, Min, Q
from django.db.models.functions import TruncDate, TruncWeek

from core.models import ObjectChange
from netbox.plugins import get_plugin_config

from .filters import KIND_AUTOMATION, KIND_PERSON, changeset_changelog_url, local_tz
from .folding import annotate_fold

__all__ = (
    'Insights',
)

ACTIONS = ('create', 'update', 'delete')
CACHE_VERSION = 1


def _action_counts(prefix=''):
    return {
        f'{prefix}{a}': Count('pk', filter=Q(action=a)) for a in ACTIONS
    }


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

    def kind_of(self, user_id, automation_ids):
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
        primary = self.primary()
        automation_ids = self.automation_user_ids()

        totals = records.aggregate(
            records=Count('pk'),
            change_sets=Count('request_id', distinct=True),
            **_action_counts(),
        )
        prim = primary.aggregate(
            records=Count('pk'),
            change_sets=Count('request_id', distinct=True),
            **_action_counts(),
        )
        # Distinct (type, id) pairs: count per type and sum.
        prim['objects'] = sum(
            row['n'] for row in primary.values('changed_object_type_id').annotate(
                n=Count('changed_object_id', distinct=True)
            ).order_by()
        )
        totals['folded'] = totals['records'] - prim['records']

        users = self._users(primary, records, automation_ids)
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
            'top_types': self._top_types(primary),
            'top_objects': self._top_objects(primary),
            'daily': self._daily(records),
        }

    def _users(self, primary, records, automation_ids):
        """Users with change sets in the period, most active first."""
        rows = primary.values('user_id', 'user_name').annotate(
            change_sets=Count('request_id', distinct=True),
            **_action_counts(),
        ).order_by()
        rec_rows = records.values('user_id', 'user_name').annotate(records=Count('pk')).order_by()
        rec_by_key = defaultdict(int)
        for row in rec_rows:
            rec_by_key[self._user_key(row)] += row['records']

        # A user renamed during the period appears under several names: merge by id.
        users = {}
        for row in rows:
            key = self._user_key(row)
            u = users.setdefault(key, {
                'user_id': row['user_id'],
                'user_name': row['user_name'],
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

    @staticmethod
    def _user_key(row):
        return ('id', row['user_id']) if row['user_id'] else ('name', row['user_name'])

    def _user_changelog_url(self, u, **extra):
        if u['user_id']:
            return self.flt.changelog_url(user_id=u['user_id'], **extra)
        return self.flt.changelog_url(user_name=u['user_name'], **extra)

    def _top_types(self, primary, limit=10):
        rows = list(primary.values('changed_object_type_id').annotate(
            change_sets=Count('request_id', distinct=True),
            records=Count('pk'),
            **_action_counts(),
        ).order_by('-change_sets', '-records')[:limit])
        types = ContentType.objects.in_bulk([r['changed_object_type_id'] for r in rows])
        for r in rows:
            r['type'] = types.get(r['changed_object_type_id'])
            r['changelog_url'] = self.flt.changelog_url(changed_object_type_id=r['changed_object_type_id'])
        return rows

    def _top_objects(self, primary, limit=10):
        rows = list(primary.values('changed_object_type_id', 'changed_object_id').annotate(
            change_sets=Count('request_id', distinct=True),
            records=Count('pk'),
            last=Max('pk'),
        ).order_by('-change_sets', '-records', '-last')[:limit])
        last = ObjectChange.objects.in_bulk([r['last'] for r in rows])
        types = ContentType.objects.in_bulk([r['changed_object_type_id'] for r in rows])
        for r in rows:
            rec = last.get(r['last'])
            r['type'] = types.get(r['changed_object_type_id'])
            r['repr'] = rec.object_repr if rec else ''
            r['deleted'] = rec is not None and rec.action == 'delete'
            r['object_url'] = None
            if rec is not None and not r['deleted'] and rec.changed_object is not None \
                    and hasattr(rec.changed_object, 'get_absolute_url'):
                r['object_url'] = rec.changed_object.get_absolute_url()
            r['changelog_url'] = self.flt.changelog_url(
                changed_object_type_id=r['changed_object_type_id'],
                changed_object_id=r['changed_object_id'],
            )
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

    # Recent change sets

    def recent_change_sets(self, limit=20):
        rows = list(self.primary().values('request_id').annotate(
            time=Min('time'),
            primary=Count('pk'),
            first=Min('pk'),
        ).order_by('-time')[:limit])
        ids = [r['request_id'] for r in rows]
        totals = dict(self.scope.filter(request_id__in=ids).values('request_id').annotate(
            n=Count('pk')
        ).order_by().values_list('request_id', 'n'))
        first = ObjectChange.objects.select_related('changed_object_type', 'user').in_bulk(
            [r['first'] for r in rows]
        )
        for r in rows:
            r['records'] = totals.get(r['request_id'], 0)
            r['folded'] = r['records'] - r['primary']
            r['first'] = first.get(r['first'])
            r['changelog_url'] = changeset_changelog_url(r['request_id'])
        return rows

    def change_set(self, request_id):
        """All records of one change set, annotated with fold_parent, in record order."""
        qs = self.scope.filter(request_id=request_id).select_related('changed_object_type', 'user')
        return list(annotate_fold(qs, self.scope).order_by('pk'))

    # Contributors

    def contributors(self):
        return self._cached('contributors', self._contributors)

    def _contributors(self):
        records = self.records()
        primary = self.primary()
        automation_ids = self.automation_user_ids()
        users = self._users(primary, records, automation_ids)
        keys = [self._user_key(u) for u in users]

        # First and last change over the whole changelog in scope.
        span = {}
        for row in self.scope.values('user_id', 'user_name').annotate(
            first=Min('time'), last=Max('time'),
        ).order_by():
            key = self._user_key(row)
            if key in keys:
                s = span.setdefault(key, {'first': row['first'], 'last': row['last']})
                s['first'] = min(s['first'], row['first'])
                s['last'] = max(s['last'], row['last'])

        # Change sets per week, weeks start on Monday in TIME_ZONE.
        weeks = []
        w = self.flt.start_date - timedelta(days=self.flt.start_date.weekday())
        while w <= self.flt.end_date:
            weeks.append(w)
            w += timedelta(days=7)
        weekly = defaultdict(lambda: defaultdict(int))
        for row in primary.annotate(week=TruncWeek('time', tzinfo=self.tz)).values(
            'user_id', 'user_name', 'week'
        ).annotate(n=Count('request_id', distinct=True)).order_by():
            week = row['week'].date() if hasattr(row['week'], 'date') else row['week']
            weekly[self._user_key(row)][week] += row['n']

        # Top types per user.
        types = defaultdict(list)
        for row in primary.values('user_id', 'user_name', 'changed_object_type_id').annotate(
            n=Count('pk')
        ).order_by('-n'):
            types[self._user_key(row)].append((row['changed_object_type_id'], row['n']))
        ct = ContentType.objects.in_bulk({t for v in types.values() for t, _ in v})

        for u in users:
            key = self._user_key(u)
            u.update(span.get(key, {'first': None, 'last': None}))
            u['weekly'] = [weekly[key].get(w, 0) for w in weeks]
            merged = defaultdict(int)
            for t, n in types[key]:
                merged[t] += n
            top = sorted(merged.items(), key=lambda kv: -kv[1])[:3]
            u['top_types'] = [
                {
                    'type': ct.get(t), 'records': n,
                    'changelog_url': self._user_changelog_url(u, changed_object_type_id=t),
                }
                for t, n in top
            ]
            u['action_urls'] = {
                a: self._user_changelog_url(u, action=a) for a in ACTIONS
            }
        return {'users': users, 'weeks': weeks}
