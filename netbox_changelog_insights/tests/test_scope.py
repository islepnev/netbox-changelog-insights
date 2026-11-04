from datetime import timedelta

from django.contrib.contenttypes.models import ContentType
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core.models import ObjectChange
from dcim.models import Cable, Device, Interface, Site
from ipam.models import IPAddress
from tenancy.models import Tenant
from users.models import User
from virtualization.models import Cluster, ClusterType, VirtualMachine, VMInterface

from netbox_changelog_insights.filters import InsightsFilter
from netbox_changelog_insights.insights import Insights
from netbox_changelog_insights.scope import scope_expression

from .test_views import PLUGINS_CONFIG
from .utils import change_set, make_inventory

SCOPE = 'plugins:netbox_changelog_insights:scope'
ACTIVITY_API = 'plugins-api:netbox_changelog_insights-api:activity'


def scope_of(rid, scope):
    """{(model, object_repr): site or tenant id} of the records of one change set."""
    qs = ObjectChange.objects.filter(request_id=rid).annotate(s=scope_expression(scope))
    return {(r.changed_object_type.model, r.object_repr): r.s for r in qs}


@override_settings(PLUGINS_CONFIG=PLUGINS_CONFIG)
class ScopeTestCase(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.inv = make_inventory()
        inv = cls.inv
        cls.site2 = Site.objects.create(name='Site 2', slug='site-2')
        cls.t1 = Tenant.objects.create(name='Tenant 1', slug='tenant-1')
        cls.t2 = Tenant.objects.create(name='Tenant 2', slug='tenant-2')
        cls.user = User.objects.create_user(username='alice', is_superuser=True)
        # A device in site 2 with tenant 1: the device and its interfaces.
        with change_set(cls.user) as rid:
            cls.d3 = Device.objects.create(name='d3', device_type=inv['dt'], role=inv['role'], site=cls.site2,
                                           tenant=cls.t1)
        cls.create_rid = rid
        # An IP address on an interface of d3, without a tenant of its own; one with tenant 2.
        iface = Interface.objects.filter(device=cls.d3).first()
        with change_set(cls.user) as rid:
            IPAddress.objects.create(address='10.0.0.1/24', assigned_object=iface)
            IPAddress.objects.create(address='10.0.0.2/24', assigned_object=iface, tenant=cls.t2)
        cls.ip_rid = rid
        # A cable from d1 (site 1) to d3 (site 2).
        a = Interface.objects.filter(device=inv['d1']).first()
        with change_set(cls.user) as rid:
            Cable(a_terminations=[a], b_terminations=[iface]).save()
        cls.cable_rid = rid
        # A virtual machine in site 1 with an interface.
        cluster = Cluster.objects.create(name='c1', type=ClusterType.objects.create(name='ct', slug='ct'))
        with change_set(cls.user) as rid:
            vm = VirtualMachine.objects.create(name='vm1', cluster=cluster, site=inv['site'], tenant=cls.t2)
            VMInterface.objects.create(virtual_machine=vm, name='eth0')
        cls.vm_rid = rid
        # An interface update of d3, then d3 is deleted: the update keeps site and tenant.
        with change_set(cls.user) as rid:
            iface2 = Interface.objects.filter(device=cls.d3).last()
            iface2.snapshot()
            iface2.description = 'x'
            iface2.save()
        cls.update_rid = rid

    def test_resolution(self):
        site1, site2 = self.inv['site'].pk, self.site2.pk
        sites = scope_of(self.create_rid, 'site')
        self.assertEqual(sites[('device', 'd3')], site2)
        self.assertEqual(sites[('interface', 'eth1')], site2)
        tenants = scope_of(self.create_rid, 'tenant')
        self.assertEqual(set(tenants.values()), {self.t1.pk})
        # IP addresses: the site of the interface's device; an own tenant first, else the device's.
        self.assertEqual(set(scope_of(self.ip_rid, 'site').values()), {site2})
        self.assertEqual(scope_of(self.ip_rid, 'tenant'), {
            ('ipaddress', '10.0.0.1/24'): self.t1.pk, ('ipaddress', '10.0.0.2/24'): self.t2.pk,
        })
        # A cable: the site of its first termination in the change set.
        cable = [v for (m, _), v in scope_of(self.cable_rid, 'site').items() if m == 'cable']
        self.assertEqual(cable, [site1])
        # A virtual machine and its interface.
        self.assertEqual(set(scope_of(self.vm_rid, 'site').values()), {site1})
        self.assertEqual(set(scope_of(self.vm_rid, 'tenant').values()), {self.t2.pk})

    def test_deleted_device(self):
        with change_set(self.user):
            self.d3.delete()
        # The tenant of an older interface update comes from the latest record of the deleted device.
        self.assertEqual(set(scope_of(self.update_rid, 'tenant').values()), {self.t1.pk})
        self.assertEqual(set(scope_of(self.update_rid, 'site').values()), {self.site2.pk})

    def insights(self, **params):
        flt = InsightsFilter.from_params({'period': '7', **params})
        return Insights(self.user, flt)

    def test_filter(self):
        # Site 2: the device, the IP addresses, the cable is in site 1 (its first termination), the update.
        primary = self.insights(site_id=self.site2.pk).primary()
        self.assertEqual(sorted(set(primary.values_list('request_id', flat=True))),
                         sorted([self.create_rid, self.ip_rid, self.update_rid]))
        pulse = self.insights(tenant_id=self.t2.pk).pulse()
        types = {t['type_label'] for t in pulse['top_types']}
        self.assertEqual(types, {'IPAM > IP Address', 'Virtualization > Virtual Machine'})

    def test_page(self):
        self.client.force_login(self.user)
        r = self.client.get(reverse(SCOPE), {'period': '7'})
        self.assertEqual(r.status_code, 200)
        groups = {g['name']: g for g in r.context['groups']}
        self.assertEqual(groups['Site 2']['change_sets'], 3)
        self.assertEqual(groups['Site 2']['create'], 3)       # the device and two IP addresses
        self.assertEqual(groups['Site 1']['change_sets'], 2)  # the cable and the VM
        self.assertNotIn(None, groups)
        r = self.client.get(reverse(SCOPE), {'period': '7', 'by': 'tenant'})
        groups = {g['name']: g for g in r.context['groups']}
        self.assertEqual(groups['Tenant 1']['change_sets'], 3)
        self.assertEqual(groups['Tenant 2']['change_sets'], 2)
        self.assertEqual(groups[None]['change_sets'], 1)       # the cable: no tenant
        self.assertContains(r, 'No tenant')
        # A row links to Pulse filtered by it; the filter shows as a badge.
        r = self.client.get(reverse('plugins:netbox_changelog_insights:pulse'), {'tenant_id': self.t1.pk})
        self.assertContains(r, 'Tenant: Tenant 1')
        self.assertEqual(r.context['pulse']['primary']['change_sets'], 3)

    def test_other_pages_with_filter(self):
        self.client.force_login(self.user)
        for name in ('contributors', 'activity', 'hotspots', 'chains'):
            r = self.client.get(reverse(f'plugins:netbox_changelog_insights:{name}'), {'site_id': self.site2.pk})
            self.assertEqual(r.status_code, 200, name)
        r = self.client.get(reverse('plugins:netbox_changelog_insights:chains'), {'site_id': self.inv['site'].pk})
        roots = {c['root_repr'] for c in r.context['chains']}
        self.assertNotIn('d3', roots)
        self.assertIn('d1', roots)

    def test_api(self):
        self.client.force_login(self.user)
        rows = self.client.get(reverse(ACTIVITY_API), {'group': 'tenant', 'period': '7'}).json()
        by_name = {r['tenant']: r for r in rows}
        self.assertEqual(by_name['Tenant 1']['change_sets'], 3)
        self.assertEqual(by_name['Tenant 1']['tenant_id'], self.t1.pk)
        self.assertIn(None, by_name)
        rows = self.client.get(reverse(ACTIVITY_API), {'group': 'day', 'period': '7', 'site_id': self.site2.pk}).json()
        self.assertEqual(sum(r['change_sets'] for r in rows), 3)
