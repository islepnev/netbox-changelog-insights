from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse

from core.models import ObjectChange
from dcim.models import Cable, Device, Interface
from ipam.models import IPAddress
from users.models import Group, User

from netbox_changelog_insights.filters import InsightsFilter
from netbox_changelog_insights.insights import Insights

from .utils import change_set, make_inventory

PLUGINS_CONFIG = {'netbox_changelog_insights': {'automation_group': 'automation', 'cache_ttl': 0}}
T0 = datetime(2026, 10, 1, 9, 0, tzinfo=ZoneInfo('UTC'))
NOW = datetime(2026, 11, 4, 12, 0, tzinfo=ZoneInfo('UTC'))


@override_settings(PLUGINS_CONFIG=PLUGINS_CONFIG, TIME_ZONE='UTC')
class ChainTestCase(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.inv = make_inventory()
        cls.person = User.objects.create_user(username='alice')
        cls.bot = User.objects.create_user(username='bot')
        cls.bot.groups.add(Group.objects.create(name='automation'))
        cls.admin = User.objects.create_superuser(username='admin2')

    def at(self, user, hours, func):
        """Run func as one change set of user, at T0 + hours."""
        with change_set(user) as rid:
            func()
        ObjectChange.objects.filter(request_id=rid).update(time=T0 + timedelta(hours=hours))
        return rid

    def chains(self, **query):
        f = InsightsFilter.from_request(RequestFactory().get('/', {'period': '90', **query}), now=NOW)
        return Insights(self.admin, f).chains()

    def chains_of(self, data, name):
        return sorted((c for c in data['chains'] if c['root_repr'] == name), key=lambda c: c['start'])

    def new_device(self, name):
        return Device.objects.create(name=name, device_type=self.inv['dt'], role=self.inv['role'], site=self.inv['site'])

    def update(self, obj, **fields):
        obj = type(obj).objects.get(pk=obj.pk)
        obj.snapshot()
        for k, v in fields.items():
            setattr(obj, k, v)
        obj.save()

    def cable(self, dev, other):
        a = Interface.objects.filter(device=dev, cable__isnull=True).first()
        b = Interface.objects.filter(device=other, cable__isnull=True).first()
        Cable(a_terminations=[a], b_terminations=[b]).save()

    def test_onboarding_over_days(self):
        """Create, fields, cables (person after 30 h, bot after 3 h more), IP days later: one chain."""
        box = {}
        self.at(self.person, 0, lambda: box.update(dev=self.new_device('srv1')))
        dev = box['dev']
        self.at(self.person, 2, lambda: self.update(dev, serial='S1', rack=None))
        self.at(self.person, 32, lambda: self.cable(dev, self.inv['d1']))
        self.at(self.bot, 35, lambda: self.cable(dev, self.inv['d2']))
        iface = Interface.objects.filter(device=dev).last()

        def ip():
            addr = IPAddress.objects.create(address='10.1.0.1/24', assigned_object=iface)
            self.update(dev, primary_ip4=addr)
        self.at(self.person, 35 + 4 * 24, ip)
        (chain,) = self.chains_of(self.chains(), 'srv1')
        self.assertTrue(chain['onboarding'])
        self.assertEqual(chain['change_sets'], 5)
        self.assertEqual([s['key'] for s in chain['stages']], ['create', 'metadata', 'cabling', 'ip'])
        self.assertEqual([u['name'] for u in chain['users']], ['alice', 'bot'])
        self.assertIn(chain, self.chains(onboarding='1')['chains'])

    def test_automation_joins_but_does_not_extend(self):
        dev = self.new_device('srv2')
        self.at(self.person, 0, lambda: self.update(dev, serial='A'))
        self.at(self.bot, 3 * 24, lambda: self.update(dev, description='sync'))      # a person follows: joins
        self.at(self.person, 5 * 24, lambda: self.update(dev, serial='B'))
        self.at(self.bot, 8 * 24, lambda: self.update(dev, description='sync 2'))    # no person after: new chain
        self.at(self.person, 20 * 24, lambda: self.update(dev, serial='C'))          # gap over 7 d: new chain
        chains = self.chains_of(self.chains(), 'srv2')
        self.assertEqual([c['change_sets'] for c in chains], [3, 1, 1])
        self.assertFalse(chains[0]['onboarding'])

    def test_cable_belongs_to_both_devices(self):
        d1, d2 = self.inv['d1'], self.inv['d2']
        self.at(self.person, 0, lambda: self.cable(d1, d2))
        data = self.chains()
        for name in ('d1', 'd2'):
            (chain,) = self.chains_of(data, name)
            self.assertEqual([s['key'] for s in chain['stages']], ['cabling'])

    def test_filters_and_page(self):
        dev = self.new_device('srv3')
        self.at(self.bot, 1, lambda: self.update(dev, serial='X'))
        self.assertEqual(self.chains_of(self.chains(kind='person'), 'srv3'), [])
        self.assertEqual(len(self.chains_of(self.chains(kind='automation'), 'srv3')), 1)
        self.assertEqual(self.chains_of(self.chains(user_id=str(self.person.pk)), 'srv3'), [])
        self.client.force_login(self.admin)
        r = self.client.get(reverse('plugins:netbox_changelog_insights:chains'), {'period': '90'})
        self.assertEqual(r.status_code, 200)
        nobody = User.objects.create_user(username='nobody')
        self.client.force_login(nobody)
        self.assertEqual(self.client.get(reverse('plugins:netbox_changelog_insights:chains')).status_code, 403)
