"""The plugin keeps working when the changelog holds records of a disabled or removed plugin."""
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from django.contrib.contenttypes.models import ContentType
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse

from core.models import ObjectChange
from users.models import User

from netbox_changelog_insights.filters import InsightsFilter
from netbox_changelog_insights.insights import Insights
from netbox_changelog_insights.labels import type_label

from .utils import make_inventory

NOW = datetime(2026, 11, 4, 12, tzinfo=ZoneInfo('UTC'))


@override_settings(PLUGINS_CONFIG={'netbox_changelog_insights': {'cache_ttl': 0}}, TIME_ZONE='UTC')
class OtherPluginTestCase(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.inv = make_inventory()
        cls.user = User.objects.create_superuser(username='admin2')
        # A model of a plugin that is not installed: the content type stays, the model is gone.
        cls.ct = ContentType.objects.create(app_label='netbox_inventory', model='asset')
        cls.rid = uuid.uuid4()
        ObjectChange.objects.create(
            changed_object_type=cls.ct, changed_object_id=7, action='update', user=cls.user,
            user_name='admin2', request_id=cls.rid, object_repr='Asset 7',
            prechange_data={'device': cls.inv['d1'].pk, 'serial': ''},
            postchange_data={'device': cls.inv['d1'].pk, 'serial': 'X1'},
        )
        ObjectChange.objects.filter(request_id=cls.rid).update(time=NOW)

    def test_label_without_model(self):
        self.assertIsNone(self.ct.model_class())
        self.assertEqual(type_label(self.ct), 'netbox_inventory > asset')

    def test_pages_and_chains(self):
        f = InsightsFilter.from_request(RequestFactory().get('/', {'period': '7'}), now=NOW)
        pulse = Insights(self.user, f).pulse()
        self.assertEqual([t['type_label'] for t in pulse['top_types']], ['netbox_inventory > asset'])
        Insights(self.user, f).contributors()
        chains = Insights(self.user, f).chains()['chains']
        # The record still finds its device through its data, and counts as an asset stage.
        (chain,) = chains
        self.assertEqual(chain['root_repr'], 'd1')
        self.assertEqual([s['key'] for s in chain['stages']], ['assets'])
        self.client.force_login(self.user)
        for name in ('pulse', 'contributors', 'chains'):
            self.assertEqual(self.client.get(reverse(f'plugins:netbox_changelog_insights:{name}')).status_code, 200)
        url = reverse('plugins:netbox_changelog_insights:changeset', kwargs={'request_id': self.rid})
        self.assertEqual(self.client.get(url, HTTP_HX_REQUEST='true').status_code, 200)
