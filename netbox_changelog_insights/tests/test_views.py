from datetime import datetime
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

from django.contrib.contenttypes.models import ContentType
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse

from core.models import ObjectChange
from dcim.models import Cable, Device, Interface
from users.models import Group, ObjectPermission, User

from netbox_changelog_insights.filters import InsightsFilter
from netbox_changelog_insights.insights import Insights

from .utils import change_set, make_inventory

PULSE = 'plugins:netbox_changelog_insights:pulse'
CONTRIBUTORS = 'plugins:netbox_changelog_insights:contributors'
PLUGINS_CONFIG = {'netbox_changelog_insights': {'automation_group': 'automation', 'cache_ttl': 0}}


class FilterTestCase(TestCase):

    def flt(self, query, now):
        return InsightsFilter.from_request(RequestFactory().get('/', query), now=now)

    @override_settings(TIME_ZONE='Europe/Moscow')
    def test_period_in_time_zone(self):
        tz = ZoneInfo('Europe/Moscow')
        # 2026-11-04 23:30 in Moscow is 20:30 UTC.
        now = datetime(2026, 11, 4, 20, 30, tzinfo=ZoneInfo('UTC'))
        f = self.flt({'period': '7'}, now)
        self.assertEqual(f.start, datetime(2026, 10, 29, tzinfo=tz))
        self.assertEqual(f.end, datetime(2026, 11, 5, tzinfo=tz))
        self.assertEqual(f.days, 7)

    @override_settings(TIME_ZONE='UTC')
    def test_custom_and_invalid(self):
        now = datetime(2026, 11, 4, 12, tzinfo=ZoneInfo('UTC'))
        f = self.flt({'period': 'custom', 'start': '2026-09-01', 'end': '2026-09-30'}, now)
        self.assertEqual((f.start_date.isoformat(), f.end_date.isoformat(), f.days), ('2026-09-01', '2026-09-30', 30))
        # Start after end, unknown period, bad numbers: default period, no exception.
        for q in ({'period': 'custom', 'start': '2026-10-01', 'end': '2026-09-01'},
                  {'period': 'bogus', 'type_id': 'abc'}):
            f = self.flt(q, now)
            self.assertEqual(f.days, 30)
        self.assertEqual(sorted(f.errors), ['period', 'type_id'])
        self.assertIsNone(f.type_id)

    @override_settings(TIME_ZONE='UTC')
    def test_max_period_days(self):
        now = datetime(2026, 11, 4, 12, tzinfo=ZoneInfo('UTC'))
        q = {'period': 'custom', 'start': '2020-01-06', 'end': '2026-11-04'}
        f = self.flt(q, now)
        self.assertEqual((f.start_date.isoformat(), f.cut_from), ('2020-01-06', None))
        config = {'netbox_changelog_insights': {'max_period_days': 365}}
        with override_settings(PLUGINS_CONFIG=config):
            f = self.flt(q, now)
            self.assertEqual((f.start_date.isoformat(), f.end_date.isoformat(), f.days), ('2025-11-05', '2026-11-04', 365))
            self.assertEqual(f.cut_from.isoformat(), '2020-01-06')
            self.assertIsNone(self.flt({'period': '90'}, now).cut_from)

    @override_settings(TIME_ZONE='UTC')
    def test_changelog_url(self):
        now = datetime(2026, 11, 4, 12, tzinfo=ZoneInfo('UTC'))
        f = self.flt({'period': '7', 'user_id': '5', 'action': 'create', 'type_id': '9'}, now)
        q = parse_qs(urlparse(f.changelog_url()).query)
        self.assertEqual(urlparse(f.changelog_url()).path, reverse('core:objectchange_list'))
        self.assertEqual(q, {
            'time_after': ['2026-10-29 00:00:00'], 'time_before': ['2026-11-04 23:59:59'],
            'user_id': ['5'], 'changed_object_type_id': ['9'], 'action': ['create'],
        })


@override_settings(PLUGINS_CONFIG=PLUGINS_CONFIG, LOGIN_REQUIRED=True, EXEMPT_VIEW_PERMISSIONS=[])
class ViewTestCase(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.inv = make_inventory()
        group = Group.objects.create(name='automation')
        cls.bot = User.objects.create_user(username='bot')
        cls.bot.groups.add(group)
        cls.alice = User.objects.create_user(username='alice')
        cls.gone = User.objects.create_user(username='gone')
        inv = cls.inv
        # alice: a device (1 primary, 3 folded) and an interface update.
        with change_set(cls.alice):
            Device.objects.create(name='d3', device_type=inv['dt'], role=inv['role'], site=inv['site'])
        iface = Interface.objects.filter(device=inv['d1']).first()
        with change_set(cls.alice):
            iface.snapshot()
            iface.description = 'x'
            iface.save()
        # bot: a cable (1 primary, 5 folded).
        a = Interface.objects.filter(device=inv['d1']).last()
        b = Interface.objects.filter(device=inv['d2']).last()
        with change_set(cls.bot) as rid:
            Cable(a_terminations=[a], b_terminations=[b]).save()
        cls.cable_rid = rid
        # gone: an interface update, then the account is deleted.
        with change_set(cls.gone):
            iface.snapshot()
            iface.description = 'y'
            iface.save()
        cls.gone.delete()

        cls.viewer = User.objects.create_user(username='viewer')
        perm = ObjectPermission.objects.create(name='view changes', actions=['view'])
        perm.object_types.add(ContentType.objects.get_for_model(ObjectChange))
        perm.users.add(cls.viewer)
        cls.nobody = User.objects.create_user(username='nobody')

    def test_permission_required(self):
        url = reverse(PULSE)
        r = self.client.get(url)
        self.assertEqual(r.status_code, 302)  # login
        self.client.force_login(self.nobody)
        for name in (PULSE, CONTRIBUTORS):
            self.assertEqual(self.client.get(reverse(name)).status_code, 403)
        self.client.force_login(self.viewer)
        for name in (PULSE, CONTRIBUTORS):
            self.assertEqual(self.client.get(reverse(name)).status_code, 200)

    def test_pulse_numbers(self):
        self.client.force_login(self.viewer)
        r = self.client.get(reverse(PULSE), {'period': '7'})
        p = r.context['pulse']
        self.assertEqual(p['totals']['change_sets'], 4)
        self.assertEqual(p['totals']['records'], 4 + 1 + 6 + 1)
        self.assertEqual(p['primary']['records'], 4)
        self.assertEqual(p['totals']['folded'], 8)
        self.assertEqual(p['kinds'], {'person': 2, 'automation': 1})
        users = {u['user_name']: u for u in p['top_users']}
        self.assertEqual(users['alice']['change_sets'], 2)
        self.assertTrue(users['gone']['deleted'])
        types = {r['type_label']: r['records'] for r in p['top_types']}
        self.assertEqual(types, {'DCIM > Device': 1, 'DCIM > Interface': 2, 'DCIM > Cable': 1})
        self.assertEqual(sum(d['change_sets'] for d in p['daily']), 4)
        self.assertContains(r, reverse('core:objectchange_list'))

    def test_filters(self):
        self.client.force_login(self.viewer)
        p = self.client.get(reverse(PULSE), {'kind': 'automation'}).context['pulse']
        self.assertEqual((p['totals']['change_sets'], p['primary']['records']), (1, 1))
        p = self.client.get(reverse(PULSE), {'user_name': 'gone'}).context['pulse']
        self.assertEqual(p['totals']['records'], 1)
        ct = ContentType.objects.get_for_model(Interface)
        p = self.client.get(reverse(PULSE), {'type_id': ct.pk}).context['pulse']
        # Change sets with a primary interface change: alice's and gone's updates.
        self.assertEqual(p['primary']['change_sets'], 2)
        p = self.client.get(reverse(PULSE), {'period': 'custom', 'start': '2020-01-01', 'end': '2020-01-02'}).context['pulse']
        self.assertEqual(p['totals']['records'], 0)
        r = self.client.get(reverse(PULSE), {'type_id': 'abc', 'user_id': str(self.alice.pk)})
        self.assertContains(r, 'alert-warning')
        self.assertEqual(r.context['pulse']['totals']['change_sets'], 2)

    def test_period_cut_message(self):
        self.client.force_login(self.viewer)
        q = {'period': 'custom', 'start': '2020-01-01'}
        self.assertNotContains(self.client.get(reverse(PULSE), q), 'max_period_days')
        config = {'netbox_changelog_insights': dict(PLUGINS_CONFIG['netbox_changelog_insights'], max_period_days=30)}
        with override_settings(PLUGINS_CONFIG=config):
            r = self.client.get(reverse(PULSE), q)
            self.assertContains(r, 'max_period_days')
            self.assertEqual(r.context['flt'].days, 30)
            self.assertEqual(r.context['pulse']['totals']['change_sets'], 4)

    def test_activity(self):
        url = reverse('plugins:netbox_changelog_insights:activity')
        self.client.force_login(self.nobody)
        self.assertEqual(self.client.get(url).status_code, 403)
        self.client.force_login(self.viewer)
        r = self.client.get(url, {'period': '7'})
        self.assertEqual(r.status_code, 200)
        groups = {g['key']: g['total'] for g in r.context['chart']['groups']}
        # Primary changes: a device and a cable created, two interface updates.
        self.assertEqual(groups, {'create': 2, 'update': 2})
        self.assertEqual(r.context['bucket'], 'day')
        self.assertEqual(len(r.context['chart']['bars']), 7)
        self.assertEqual(sum(b['change_sets'] for b in r.context['chart']['bars']), 4)
        self.assertEqual(r.context['heat']['total'], 4)
        r = self.client.get(url, {'period': '90', 'by': 'user'})
        labels = {g['label']: g['total'] for g in r.context['chart']['groups']}
        self.assertEqual(labels, {'alice': 2, 'bot': 1, 'gone': 1})
        # A user group drills down to object types of that user.
        alice = next(g for g in r.context['chart']['groups'] if g['label'] == 'alice')
        self.assertIn('by=model', alice['filter_url'])
        self.assertIn(f'user_id={self.alice.pk}', alice['filter_url'])
        r = self.client.get(url, {'by': 'model', 'bucket': 'week', 'kind': 'automation'})
        self.assertEqual([g['label'] for g in r.context['chart']['groups']], ['DCIM > Cable'])
        self.assertTrue(all(b['start'].weekday() == 0 for b in r.context['chart']['bars']))
        r = self.client.get(url, {'by': 'app'})
        self.assertEqual([g['label'] for g in r.context['chart']['groups']], ['DCIM'])
        # The full history of a long period falls back to weeks.
        r = self.client.get(url, {'period': 'custom', 'start': '2020-01-01'})
        self.assertEqual(r.context['bucket'], 'week')

    def test_hotspots(self):
        url = reverse('plugins:netbox_changelog_insights:hotspots')
        self.client.force_login(self.nobody)
        self.assertEqual(self.client.get(url).status_code, 403)
        self.client.force_login(self.viewer)
        r = self.client.get(url, {'period': '7'})
        self.assertEqual(r.status_code, 200)
        objects = {(o['type_label'], o['repr']): o for o in r.context['objects']}
        # The interface updated by alice and by the deleted user: 2 change sets, 2 users.
        iface = Interface.objects.filter(device=self.inv['d1']).first()
        o = objects[('DCIM > Interface', iface.name)]
        self.assertEqual((o['change_sets'], o['updates'], o['users'], o['parent']), (2, 2, 2, 'd1'))
        # Fields of primary updates only: the cable fields of the bot's cable are folded.
        fields = {(f['type_label'], f['field']): f['updates'] for f in r.context['fields']}
        self.assertEqual(fields, {('DCIM > Interface', 'description'): 2})

        ct = ContentType.objects.get_for_model(Interface)
        changes = reverse('plugins:netbox_changelog_insights:field_changes')
        r = self.client.get(changes, {'type_id': ct.pk, 'field': 'description'})
        self.assertEqual(r.context['total'], 2)
        self.assertEqual([(x['before'], x['after']) for x in r.context['rows']], [('"x"', '"y"'), ('""', '"x"')])
        self.assertEqual(r.context['rows'][0]['parent'], 'd1')
        self.assertEqual(self.client.get(changes, {'field': 'description'}).status_code, 404)
        r = self.client.get(changes, {'type_id': ct.pk, 'field': 'description', 'user_name': 'gone'})
        self.assertEqual(r.context['total'], 1)

    def test_activity_other_group(self):
        from netbox_changelog_insights.charts import stacked_chart
        from datetime import date
        data = {
            'groups': [{'key': str(i), 'label': str(i), 'total': 1, 'params': {}} for i in range(6)]
            + [{'key': 'other', 'label': None, 'total': 2, 'params': None, 'count': 2}],
            'buckets': [{'start': date(2026, 11, 2), 'counts': [1] * 6 + [2], 'total': 8, 'change_sets': 8,
                         'days': 1, 'full_days': 1}],
        }
        chart = stacked_chart(data, 'user')
        self.assertEqual([g['css'] for g in chart['groups']][-2:], ['insights-s6', 'insights-other'])
        self.assertEqual(chart['bars'][0]['segments'][-1]['height'], 25)

    def test_changelog_links_keep_the_filter(self):
        """Links to the core changelog carry one action and the users of the selected kind."""
        self.client.force_login(self.viewer)
        r = self.client.get(reverse(PULSE), {'action': 'update'})
        q = parse_qs(urlparse(r.context['action_urls']['create']).query)
        self.assertEqual(q['action'], ['create'])
        r = self.client.get(reverse(PULSE), {'kind': 'automation'})
        q = parse_qs(urlparse(r.context['flt'].changelog_url()).query)
        self.assertEqual(q['user_id'], [str(self.bot.pk)])
        r = self.client.get(reverse(PULSE), {'kind': 'person'})
        q = parse_qs(urlparse(r.context['flt'].changelog_url()).query)
        self.assertEqual(q['user_id'], [str(self.alice.pk)])  # the deleted user has no id

    def test_cached_values_are_plain_data(self):
        """The cache holds no model instances, so an upgrade cannot break reading it."""
        from django.db.models import Model

        def walk(value):
            self.assertNotIsInstance(value, Model)
            if isinstance(value, dict):
                for v in value.values():
                    walk(v)
            elif isinstance(value, (list, tuple, set)):
                for v in value:
                    walk(v)

        f = InsightsFilter.from_request(RequestFactory().get('/', {'period': '30'}))
        walk(Insights(self.viewer, f).pulse())
        walk(Insights(self.viewer, f).contributors())

    def test_object_permission_constraint(self):
        """Numbers stay inside the records the user may view."""
        limited = User.objects.create_user(username='limited')
        perm = ObjectPermission.objects.create(name='alice only', actions=['view'], constraints={'user_name': 'alice'})
        perm.object_types.add(ContentType.objects.get_for_model(ObjectChange))
        perm.users.add(limited)
        self.client.force_login(limited)
        p = self.client.get(reverse(PULSE)).context['pulse']
        self.assertEqual(p['totals']['change_sets'], 2)
        self.assertEqual([u['user_name'] for u in p['top_users']], ['alice'])

    def test_contributors(self):
        self.client.force_login(self.viewer)
        r = self.client.get(reverse(CONTRIBUTORS), {'period': '30'})
        users = {u['user_name']: u for u in r.context['users']}
        self.assertEqual(set(users), {'alice', 'bot', 'gone'})
        self.assertEqual(users['bot']['kind'], 'automation')
        self.assertEqual((users['alice']['create'], users['alice']['update']), (1, 1))
        self.assertEqual(sum(users['alice']['weekly']), 2)
        self.assertEqual(len(users['alice']['weekly']), len(r.context['weeks']))
        self.assertIsNotNone(users['alice']['first'])
        self.assertContains(r, 'mdi-robot-outline')

    def test_change_set_fragment(self):
        self.client.force_login(self.viewer)
        url = reverse('plugins:netbox_changelog_insights:changeset', kwargs={'request_id': self.cable_rid})
        r = self.client.get(url, HTTP_HX_REQUEST='true')
        self.assertEqual(r.status_code, 200)
        self.assertTemplateUsed(r, 'netbox_changelog_insights/inc/changeset_tree.html')
        self.assertEqual(len(r.context['tree']), 1)
        self.assertEqual(r.context['folded'], 5)
        self.assertEqual(self.client.get(url).status_code, 200)
        url = reverse('plugins:netbox_changelog_insights:changeset',
                      kwargs={'request_id': '00000000-0000-0000-0000-000000000000'})
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_day_buckets_use_time_zone(self):
        """A change at 22:30 UTC on Nov 3 falls on Nov 4 in Moscow (UTC+3)."""
        ObjectChange.objects.update(time=datetime(2026, 11, 3, 22, 30, tzinfo=ZoneInfo('UTC')))
        now = datetime(2026, 11, 4, 12, tzinfo=ZoneInfo('UTC'))
        for tz, day in (('Europe/Moscow', '2026-11-04'), ('UTC', '2026-11-03')):
            with self.settings(TIME_ZONE=tz):
                f = InsightsFilter.from_request(RequestFactory().get('/', {'period': '7'}), now=now)
                daily = Insights(self.viewer, f).pulse()['daily']
                busy = [d['day'].isoformat() for d in daily if d['change_sets']]
                self.assertEqual(busy, [day])

    def test_cache(self):
        self.client.force_login(self.viewer)
        with self.settings(PLUGINS_CONFIG={'netbox_changelog_insights': {'automation_group': None, 'cache_ttl': 60}}):
            from django.core.cache import cache
            cache.clear()
            first = self.client.get(reverse(PULSE)).context['pulse']['totals']['records']
            ObjectChange.objects.filter(user_name='alice').delete()
            second = self.client.get(reverse(PULSE)).context['pulse']['totals']['records']
            self.assertEqual(first, second)
            cache.clear()
            third = self.client.get(reverse(PULSE)).context['pulse']['totals']['records']
            self.assertLess(third, first)


class SparklineTestCase(TestCase):

    def test_partial_weeks_are_dashed(self):
        from datetime import date
        from netbox_changelog_insights.charts import sparkline
        # Period Wed 2026-10-07 .. Wed 2026-10-28: four weeks, first and last in part.
        weeks = [date(2026, 10, 5), date(2026, 10, 12), date(2026, 10, 19), date(2026, 10, 26)]
        s = sparkline([3, 5, 4, 1], 5, weeks, date(2026, 10, 7), date(2026, 10, 28))
        self.assertEqual((len(s['solid']), len(s['dashed'])), (1, 2))
        self.assertIn('Oct 5: 3 (5 of 7 days)', s['title'])
        self.assertIn('Oct 26: 1 (3 of 7 days)', s['title'])
        self.assertIn('Oct 12: 5,', s['title'])
        full = sparkline([1, 2], 2, weeks[1:3], date(2026, 10, 12), date(2026, 10, 25))
        self.assertEqual((len(full['solid']), len(full['dashed'])), (1, 0))
