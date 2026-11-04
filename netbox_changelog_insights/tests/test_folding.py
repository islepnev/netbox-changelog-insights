from django.test import TestCase

from core.models import ObjectChange
from dcim.models import Cable, Device, Interface, Module, ModuleBayTemplate, ModuleType
from ipam.models import IPAddress
from users.models import User

from netbox_changelog_insights.folding import (
    FOLD_CABLE, FOLD_CABLE_FIELDS, FOLD_PRIMARY_IP, FOLD_RELATED, FOLD_REPEAT, annotate_fold, fold_tree,
)

from .utils import change_set, make_inventory, records


def fold(rid):
    """{(model, action, object id): parent record or None} for one change set."""
    qs = annotate_fold(records(rid), ObjectChange.objects.all()).select_related('changed_object_type')
    rows = list(qs.order_by('pk'))
    by_id = {r.pk: r for r in rows}
    return rows, by_id


def primary(rid):
    rows, _ = fold(rid)
    return sorted((r.changed_object_type.model, r.action) for r in rows if r.fold_parent is None)


class FoldingTestCase(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(username='u1')
        cls.inv = make_inventory()

    def ifaces(self, device):
        return list(Interface.objects.filter(device=device).order_by('name'))

    def test_device_create_folds_components(self):
        with change_set(self.user) as rid:
            Device.objects.create(name='d3', device_type=self.inv['dt'], role=self.inv['role'], site=self.inv['site'])
        self.assertEqual(records(rid).count(), 4)
        self.assertEqual(primary(rid), [('device', 'create')])
        rows, by_id = fold(rid)
        tree = fold_tree(rows)
        self.assertEqual(len(tree), 1)
        self.assertEqual([rule for _, rule, _ in tree[0][1]], [FOLD_RELATED] * 3)

    def test_cable_create(self):
        a, b = self.ifaces(self.inv['d1'])[0], self.ifaces(self.inv['d2'])[0]
        with change_set(self.user) as rid:
            Cable(a_terminations=[a], b_terminations=[b]).save()
        # NetBox 4.6 writes: cable create, 2 terminations, 2 interface updates, cable update.
        self.assertEqual(records(rid).count(), 6)
        self.assertEqual(primary(rid), [('cable', 'create')])
        rows, by_id = fold(rid)
        (root, folded), = fold_tree(rows)
        self.assertEqual((root.changed_object_type.model, root.action), ('cable', 'create'))
        rules = sorted(rule for _, rule, _ in folded)
        self.assertEqual(rules, sorted([FOLD_REPEAT, FOLD_CABLE, FOLD_CABLE, FOLD_CABLE_FIELDS, FOLD_CABLE_FIELDS]))

    def test_cable_retarget_and_delete(self):
        d1, d2 = self.ifaces(self.inv['d1']), self.ifaces(self.inv['d2'])
        cable = Cable(a_terminations=[d1[0]], b_terminations=[d2[0]])
        cable.save()
        cable = Cable.objects.get(pk=cable.pk)
        with change_set(self.user) as rid:
            cable.snapshot()
            cable.b_terminations = [d2[1]]
            cable.save()
        self.assertEqual(primary(rid), [('cable', 'update')])
        with change_set(self.user) as rid:
            Cable.objects.get(pk=cable.pk).delete()
        self.assertEqual(primary(rid), [('cable', 'delete')])

    def test_interface_update_is_primary(self):
        iface = self.ifaces(self.inv['d1'])[0]
        with change_set(self.user) as rid:
            iface.snapshot()
            iface.description = 'x'
            iface.save()
        self.assertEqual(primary(rid), [('interface', 'update')])

    def test_bulk_interface_update_stays_primary(self):
        with change_set(self.user) as rid:
            for iface in self.ifaces(self.inv['d1']):
                iface.snapshot()
                iface.enabled = False
                iface.save()
        self.assertEqual(primary(rid), [('interface', 'update')] * 3)

    def test_device_delete_folds_components_and_ip(self):
        iface = self.ifaces(self.inv['d2'])[0]
        IPAddress.objects.create(address='10.0.0.1/24', assigned_object=iface)
        with change_set(self.user) as rid:
            Device.objects.get(pk=self.inv['d2'].pk).delete()
        self.assertEqual(primary(rid), [('device', 'delete')])
        self.assertEqual(records(rid).count(), 5)

    def test_update_then_delete_folds_into_delete(self):
        """NetBox updates a device before it deletes it; the change counts as a delete."""
        dev = Device.objects.create(name='d8', device_type=self.inv['dt'], role=self.inv['role'], site=self.inv['site'])
        with change_set(self.user) as rid:
            dev.snapshot()
            dev.description = 'retired'
            dev.save()
            Device.objects.get(pk=dev.pk).delete()
        self.assertEqual(primary(rid), [('device', 'delete')])
        rows, by_id = fold(rid)
        (root, folded), = fold_tree(rows)
        self.assertEqual(root.action, 'delete')
        self.assertEqual(len(folded), 4)

    def test_primary_ip_update_folds_into_ip(self):
        """"Make this the primary IP": the device update is a side effect of the IP create."""
        dev = Device.objects.get(pk=self.inv['d1'].pk)
        iface = self.ifaces(dev)[0]
        with change_set(self.user) as rid:
            ip = IPAddress.objects.create(address='10.0.0.7/24', assigned_object=iface)
            dev.snapshot()
            dev.primary_ip4 = ip
            dev.save()
        self.assertEqual(primary(rid), [('ipaddress', 'create')])
        rows, _ = fold(rid)
        (root, folded), = fold_tree(rows)
        self.assertEqual([rule for _, rule, _ in folded], [FOLD_PRIMARY_IP])
        # A device update that also changes another field stays primary.
        with change_set(self.user) as rid:
            ip2 = IPAddress.objects.create(address='10.0.0.8/24', assigned_object=iface)
            dev.snapshot()
            dev.primary_ip4 = ip2
            dev.serial = 'X1'
            dev.save()
        self.assertEqual(primary(rid), [('device', 'update'), ('ipaddress', 'create')])

    def test_ip_assigned_to_existing_interface_is_primary(self):
        iface = self.ifaces(self.inv['d1'])[0]
        with change_set(self.user) as rid:
            IPAddress.objects.create(address='10.0.0.2/24', assigned_object=iface)
        self.assertEqual(primary(rid), [('ipaddress', 'create')])

    def test_module_install_components_stay_primary(self):
        """Design rule: a module component relates to the device, not the module; it stays primary."""
        ModuleBayTemplate.objects.create(device_type=self.inv['dt'], name='bay1', position='1')
        dev = Device.objects.create(name='d9', device_type=self.inv['dt'], role=self.inv['role'], site=self.inv['site'])
        mt = ModuleType.objects.create(manufacturer=self.inv['mf'], model='MT')
        from dcim.models import InterfaceTemplate
        InterfaceTemplate.objects.create(module_type=mt, name='m{module}', type='10gbase-x-sfpp')
        with change_set(self.user) as rid:
            Module.objects.create(device=dev, module_bay=dev.modulebays.first(), module_type=mt)
        self.assertEqual(primary(rid), [('interface', 'create'), ('module', 'create')])

    def test_scope_hides_parent(self):
        """A record does not fold into a record outside the scope."""
        with change_set(self.user) as rid:
            Device.objects.create(name='d4', device_type=self.inv['dt'], role=self.inv['role'], site=self.inv['site'])
        scope = ObjectChange.objects.exclude(changed_object_type__model='device')
        rows = list(annotate_fold(records(rid).filter(pk__in=scope), scope))
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(r.fold_parent is None for r in rows))

    def test_tree_and_sql_agree(self):
        """Every record appears once in the tree; tree roots are the SQL primary records."""
        a, b = self.ifaces(self.inv['d1'])[1], self.ifaces(self.inv['d2'])[1]
        with change_set(self.user) as rid:
            Device.objects.create(name='d5', device_type=self.inv['dt'], role=self.inv['role'], site=self.inv['site'])
            Cable(a_terminations=[a], b_terminations=[b]).save()
        rows, by_id = fold(rid)
        tree = fold_tree(rows)
        listed = [root.pk for root, _ in tree] + [f.pk for _, folded in tree for f, _, _ in folded]
        self.assertEqual(sorted(listed), sorted(by_id))
        self.assertEqual({root.pk for root, _ in tree}, {r.pk for r in rows if r.fold_parent is None})
        self.assertEqual(primary(rid), [('cable', 'create'), ('device', 'create')])
