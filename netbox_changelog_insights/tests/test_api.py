import json
from datetime import datetime, timedelta

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from users.models import Token

from .test_views import PLUGINS_CONFIG, ViewTestCase

ACTIVITY = 'plugins-api:netbox_changelog_insights-api:activity'


@override_settings(PLUGINS_CONFIG=PLUGINS_CONFIG, LOGIN_REQUIRED=True, EXEMPT_VIEW_PERMISSIONS=[])
class ActivityAPITestCase(TestCase):
    # The same change sets as the page tests: alice 2, bot 1 (a cable), a deleted user 1.
    setUpTestData = classmethod(ViewTestCase.setUpTestData.__func__)

    def get(self, user, **params):
        token = Token.objects.create(user=user, token=Token.generate())
        header = token.get_auth_header_prefix() + token.token
        return self.client.get(reverse(ACTIVITY), params, HTTP_AUTHORIZATION=header)

    def grafana_range(self, days=7):
        now = timezone.now()
        return {'from': int((now - timedelta(days=days)).timestamp() * 1000), 'to': int(now.timestamp() * 1000)}

    def test_permission(self):
        self.assertEqual(self.client.get(reverse(ACTIVITY)).status_code, 403)
        self.assertEqual(self.get(self.nobody).status_code, 403)
        self.assertEqual(self.get(self.viewer).status_code, 200)

    def test_day_series(self):
        r = self.get(self.viewer, group='day', **self.grafana_range(6))
        rows = r.json()
        self.assertEqual(len(rows), 7)
        self.assertEqual(sum(row['change_sets'] for row in rows), 4)
        self.assertEqual(sum(row['changes'] for row in rows), 4)
        self.assertEqual(sum(row['records'] for row in rows), 12)
        self.assertEqual(sorted(rows[0]), ['change_sets', 'changes', 'created', 'deleted', 'records', 'time', 'updated'])
        datetime.fromisoformat(rows[0]['time'])

    def test_week_series(self):
        rows = self.get(self.viewer, group='week', period='90').json()
        self.assertTrue(all(datetime.fromisoformat(row['time']).weekday() == 0 for row in rows))
        self.assertEqual(sum(row['change_sets'] for row in rows), 4)

    def test_top_lists(self):
        users = {u['user']: u for u in self.get(self.viewer, group='user').json()}
        self.assertEqual(users['alice']['change_sets'], 2)
        self.assertEqual(users['bot']['kind'], 'automation')
        self.assertEqual(users['bot']['records'], 6)
        self.assertTrue(users['gone']['user_deleted'])
        types = {t['type']: t['changes'] for t in self.get(self.viewer, group='type', limit=2).json()}
        self.assertEqual(len(types), 2)
        self.assertEqual(len(self.get(self.viewer, group='user', kind='all').json()), 3)
        rows = self.get(self.viewer, group='user', kind='automation').json()
        self.assertEqual([u['user'] for u in rows], ['bot'])

    def test_period_cut_and_errors(self):
        config = {'netbox_changelog_insights': dict(PLUGINS_CONFIG['netbox_changelog_insights'], max_period_days=3)}
        with override_settings(PLUGINS_CONFIG=config):
            r = self.get(self.viewer, group='day', **self.grafana_range(30))
            self.assertEqual(len(r.json()), 3)
            self.assertIn('X-Period-Cut-From', r)
        self.assertEqual(self.get(self.viewer, group='bogus').status_code, 400)
        self.assertEqual(self.get(self.viewer, **{'from': 'x', 'to': '1'}).status_code, 400)
        r = self.get(self.viewer, type_id='abc')
        self.assertEqual(r.status_code, 400)
        self.assertIn('type_id', json.loads(r.content))
