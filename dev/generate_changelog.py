#!/usr/bin/env python3
"""Generate a synthetic NetBox changelog for development and tests.

The script makes real changes through the NetBox ORM inside a request context, so
NetBox core writes the ObjectChange records as it does for a web or API request
(a new cable writes the cable, two terminations and two interface updates). Then it
moves each change set to a time in the past.

With --scale 1: about 20,000 change sets in 90 days; most sets hold one record;
automation writes most records; cables, interfaces, cable terminations, front ports
and devices lead.

Run it inside the NetBox environment, on a development database only:

    NETBOX_ROOT=/opt/netbox/netbox python dev/generate_changelog.py --scale 0.25

It deletes no data, but adds users, devices, cables and IP addresses.
"""
import argparse
import os
import random
import sys
import time as clock
import uuid
from collections import Counter
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.environ.get('NETBOX_ROOT', '/opt/netbox/netbox'))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'netbox.settings')

import django  # noqa: E402

django.setup()

from django.conf import settings  # noqa: E402
from django.contrib.contenttypes.models import ContentType  # noqa: E402
from django.db import connection, transaction  # noqa: E402

import core.signals  # noqa: E402
from core.models import ObjectChange  # noqa: E402
from dcim.models import (  # noqa: E402
    Cable, Device, DeviceRole, DeviceType, FrontPort, FrontPortTemplate, Interface, InterfaceTemplate,
    Manufacturer, Module, ModuleBay, ModuleBayTemplate, ModuleType, PortTemplateMapping,
    RearPortTemplate, Site,
)
from ipam.models import VLAN, IPAddress, Prefix  # noqa: E402
from netbox.context_managers import event_tracking  # noqa: E402
from users.models import Group, User  # noqa: E402
from utilities.request import NetBoxFakeRequest  # noqa: E402

# Event rules are not part of the profile; skip event serialization for speed.
core.signals.enqueue_event = lambda *args, **kwargs: None

AUTOMATION_GROUP = 'automation'

# Actors: name, share of change sets, kind, active hours (local time), weekdays only.
ACTORS = (
    ('sync-bot', 0.42, 'bot', range(0, 24), False),
    ('ipam-bot', 0.10, 'bot', range(1, 5), False),
    ('alice', 0.13, 'person', range(8, 19), True),
    ('bob', 0.10, 'person', range(9, 18), True),
    ('carol', 0.08, 'person', range(9, 20), True),
    ('dmitry', 0.07, 'person', range(10, 22), False),
    ('erin', 0.04, 'person', range(8, 17), True),
    ('olga', 0.06, 'person', range(9, 18), True),  # deleted at the end: user is null
)

# Operations per actor kind: (operation, weight).
OPS = {
    'sync-bot': (
        ('interface_update', 40), ('cable_create', 30), ('cable_delete', 8), ('bulk_interface_update', 3),
        ('device_update', 8), ('cable_update', 4), ('cable_retarget', 2),
    ),
    'ipam-bot': (
        ('ip_create', 45), ('ip_update', 35), ('ip_delete', 15), ('prefix_create', 5),
    ),
    'person': (
        ('interface_update', 22), ('device_update', 14), ('cable_create', 12), ('ip_update', 8),
        ('ip_create', 6), ('vlan_create', 4), ('device_create', 7), ('patch_panel_create', 2),
        ('device_delete', 2), ('module_install', 2), ('cable_delete', 4), ('cable_retarget', 2),
        ('script_device_interfaces', 2), ('cable_update', 3), ('bulk_interface_update', 1),
    ),
}

MESSAGE_SHARE = 0.003


class World:
    """Objects that the operations pick from."""

    def __init__(self, rng):
        self.rng = rng
        self.n = 0

    def name(self, prefix):
        self.n += 1
        return f'{prefix}-{self.n:05d}'

    def setup(self, devices):
        """Create the base inventory without change records (no request context)."""
        self.site_list = []
        for i in range(1, 6):
            site, _ = Site.objects.get_or_create(name=f'Site {i}', slug=f'site-{i}')
            self.site_list.append(site)
        mf, _ = Manufacturer.objects.get_or_create(name='Generic', slug='generic')
        self.role_switch, _ = DeviceRole.objects.get_or_create(name='Switch', slug='switch', color='2196f3')
        self.role_server, _ = DeviceRole.objects.get_or_create(name='Server', slug='server', color='4caf50')
        self.role_panel, _ = DeviceRole.objects.get_or_create(name='Patch panel', slug='patch-panel', color='9e9e9e')

        self.dt_switch = self._device_type(mf, 'Switch 24', interfaces=24, uplinks=2, bays=2)
        self.dt_server = self._device_type(mf, 'Server 4', interfaces=4)
        self.dt_panel = self._panel_type(mf, 'Panel 12', ports=12)
        self.mt, created = ModuleType.objects.get_or_create(manufacturer=mf, model='Uplink module 4')
        if created:
            for i in range(1, 5):
                InterfaceTemplate.objects.create(module_type=self.mt, name=f'{{module}}/{i}', type='10gbase-x-sfpp')

        existing = Device.objects.filter(device_type__in=(self.dt_switch, self.dt_server, self.dt_panel)).count()
        for i in range(existing, devices):
            kind = self.rng.random()
            if kind < 0.5:
                dt, role = self.dt_server, self.role_server
            elif kind < 0.8:
                dt, role = self.dt_switch, self.role_switch
            else:
                dt, role = self.dt_panel, self.role_panel
            Device.objects.create(
                name=f'base-{i:04d}', device_type=dt, role=role, site=self.rng.choice(self.site_list),
            )
        self.prefix, _ = Prefix.objects.get_or_create(prefix='10.0.0.0/8')
        self.ip_counter = IPAddress.objects.count() + 1

    @staticmethod
    def _device_type(mf, model, interfaces, uplinks=0, bays=0):
        slug = model.lower().replace(' ', '-')
        dt, created = DeviceType.objects.get_or_create(manufacturer=mf, model=model, slug=slug)
        if created:
            for i in range(1, interfaces + 1):
                InterfaceTemplate.objects.create(device_type=dt, name=f'eth{i}', type='1000base-t')
            for i in range(1, uplinks + 1):
                InterfaceTemplate.objects.create(device_type=dt, name=f'xe{i}', type='10gbase-x-sfpp')
            for i in range(1, bays + 1):
                ModuleBayTemplate.objects.create(device_type=dt, name=f'bay{i}', position=str(i))
        return dt

    @staticmethod
    def _panel_type(mf, model, ports):
        slug = model.lower().replace(' ', '-')
        dt, created = DeviceType.objects.get_or_create(manufacturer=mf, model=model, slug=slug)
        if created:
            for i in range(1, ports + 1):
                rp = RearPortTemplate.objects.create(device_type=dt, name=f'rear{i}', type='lc', positions=1)
                fp = FrontPortTemplate.objects.create(device_type=dt, name=f'front{i}', type='lc', positions=1)
                PortTemplateMapping.objects.create(
                    device_type=dt, front_port=fp, front_port_position=1, rear_port=rp, rear_port_position=1,
                )
        return dt

    # Pickers

    def free_endpoint(self):
        """An interface or front port with no cable."""
        if self.rng.random() < 0.3:
            qs = FrontPort.objects.filter(cable__isnull=True)
        else:
            qs = Interface.objects.filter(cable__isnull=True, mgmt_only=False)
        return self._random(qs)

    def _random(self, qs):
        count = qs.count()
        if not count:
            return None
        return qs[self.rng.randrange(count)]


def op_interface_update(w, msg):
    iface = w._random(Interface.objects.all())
    iface.snapshot()
    field = w.rng.choice(('description', 'enabled', 'mtu', 'description'))
    if field == 'description':
        iface.description = w.rng.choice(('uplink', 'server', 'reserved', 'to core', '')) + f' {w.rng.randrange(100)}'
    elif field == 'enabled':
        iface.enabled = not iface.enabled
    else:
        iface.mtu = w.rng.choice((1500, 9000, 9216))
    iface._changelog_message = msg
    iface.save()


def op_bulk_interface_update(w, msg):
    """Many interfaces in one request, as an API bulk PATCH."""
    n = min(int(w.rng.paretovariate(1.2) * 4), 650)
    ids = list(Interface.objects.order_by('?').values_list('pk', flat=True)[:max(n, 2)])
    for iface in Interface.objects.filter(pk__in=ids):
        iface.snapshot()
        iface.description = f'audit {w.rng.randrange(1000)}'
        iface.save()


def op_device_update(w, msg):
    dev = w._random(Device.objects.all())
    dev.snapshot()
    if w.rng.random() < 0.5:
        dev.serial = f'SN{w.rng.randrange(10 ** 8):08d}'
    else:
        dev.status = w.rng.choice(('active', 'planned', 'offline', 'active'))
    dev._changelog_message = msg
    dev.save()


def op_device_create(w, msg):
    dt, role = (w.dt_switch, w.role_switch) if w.rng.random() < 0.4 else (w.dt_server, w.role_server)
    Device.objects.create(name=w.name('dev'), device_type=dt, role=role, site=w.rng.choice(w.site_list))


def op_patch_panel_create(w, msg):
    Device.objects.create(
        name=w.name('panel'), device_type=w.dt_panel, role=w.role_panel, site=w.rng.choice(w.site_list),
    )


def op_device_delete(w, msg):
    dev = w._random(Device.objects.filter(interfaces__cable__isnull=True).exclude(
        interfaces__cable__isnull=False).exclude(frontports__cable__isnull=False).distinct())
    if dev:
        dev.snapshot()
        dev.delete()


def op_module_install(w, msg):
    bay = w._random(ModuleBay.objects.filter(installed_module__isnull=True, device__device_type=w.dt_switch))
    if bay:
        Module.objects.create(device=bay.device, module_bay=bay, module_type=w.mt)


def op_script_device_interfaces(w, msg):
    """A custom script: update a device and some of its interfaces in one request."""
    dev = w._random(Device.objects.filter(device_type=w.dt_switch))
    if not dev:
        return
    dev.snapshot()
    dev.description = f'provisioned {w.rng.randrange(1000)}'
    dev.save()
    for iface in dev.interfaces.order_by('?')[:w.rng.randrange(1, 5)]:
        iface.snapshot()
        iface.description = 'provisioned'
        iface.save()


def op_cable_create(w, msg):
    a, b = w.free_endpoint(), w.free_endpoint()
    if a is None or b is None or a == b or getattr(a, 'device_id', None) == getattr(b, 'device_id', None):
        return
    cable = Cable(a_terminations=[a], b_terminations=[b], status='connected')
    cable._changelog_message = msg
    cable.save()


def op_cable_update(w, msg):
    cable = w._random(Cable.objects.all())
    if cable:
        cable.snapshot()
        cable.label = f'C{w.rng.randrange(10 ** 5):05d}'
        cable.save()


def op_cable_delete(w, msg):
    cable = w._random(Cable.objects.all())
    if cable:
        cable.snapshot()
        cable.delete()


def op_cable_retarget(w, msg):
    cable = w._random(Cable.objects.all())
    new = w.free_endpoint()
    if not cable or not new:
        return
    cable = Cable.objects.get(pk=cable.pk)
    cable.snapshot()
    if not cable.a_terminations or type(cable.a_terminations[0]) is not type(new):
        return
    cable.b_terminations = [new]
    cable.save()


def op_ip_create(w, msg):
    n = w.ip_counter
    w.ip_counter += 1
    addr = f'10.{(n >> 16) & 255}.{(n >> 8) & 255}.{n & 255}/24'
    iface = w._random(Interface.objects.all()) if w.rng.random() < 0.6 else None
    IPAddress.objects.create(address=addr, assigned_object=iface, status='active')


def op_ip_update(w, msg):
    ip = w._random(IPAddress.objects.all())
    if ip:
        ip.snapshot()
        ip.dns_name = f'host{w.rng.randrange(10 ** 4)}.example.net'
        ip.save()


def op_ip_delete(w, msg):
    ip = w._random(IPAddress.objects.all())
    if ip:
        ip.snapshot()
        ip.delete()


def op_prefix_create(w, msg):
    n = w.rng.randrange(1, 65000)
    Prefix.objects.get_or_create(prefix=f'10.{n >> 8}.{n & 255}.0/24')


def op_vlan_create(w, msg):
    vid = w.rng.randrange(2, 4094)
    if not VLAN.objects.filter(vid=vid, site=None).exists():
        VLAN.objects.create(vid=vid, name=f'vlan{vid}')


# Onboarding of a server: one device, several change sets over hours or days (see
# onboarding_events()). ctx carries the device between the steps.

def onboard_create(w, msg, ctx):
    ctx['device'] = Device.objects.create(
        name=w.name('srv'), device_type=w.dt_switch, role=w.role_server, site=w.rng.choice(w.site_list),
        status='planned',
    ).pk


def onboard_metadata(w, msg, ctx):
    dev = Device.objects.get(pk=ctx['device'])
    dev.snapshot()
    dev.serial = f'SN{w.rng.randrange(10 ** 8):08d}'
    dev.asset_tag = f'AT{w.rng.randrange(10 ** 6):06d}'
    if w.rng.random() < 0.5:
        dev.comments = 'Received and racked'
    dev.save()


def onboard_module(w, msg, ctx):
    bay = ModuleBay.objects.filter(device_id=ctx['device'], installed_module__isnull=True).first()
    if bay:
        Module.objects.create(device_id=ctx['device'], module_bay=bay, module_type=w.mt)


def onboard_cable(w, msg, ctx):
    a = Interface.objects.filter(device_id=ctx['device'], cable__isnull=True, mgmt_only=False).first()
    b = w.free_endpoint()
    if a and b and getattr(b, 'device_id', None) != ctx['device']:
        Cable(a_terminations=[a], b_terminations=[b], status='connected').save()


def onboard_ip(w, msg, ctx):
    iface = Interface.objects.filter(device_id=ctx['device']).first()
    n = w.ip_counter
    w.ip_counter += 1
    ip = IPAddress.objects.create(
        address=f'10.{128 + ((n >> 16) & 127)}.{(n >> 8) & 255}.{n & 255}/24', assigned_object=iface,
    )
    dev = Device.objects.get(pk=ctx['device'])
    dev.snapshot()
    dev.primary_ip4 = ip
    dev.save()


def onboard_activate(w, msg, ctx):
    dev = Device.objects.get(pk=ctx['device'])
    dev.snapshot()
    dev.status = 'active'
    dev.save()


# Steps: (operation, actor, earliest and latest offset from the create, in hours, probability).
ONBOARDING = (
    (onboard_metadata, 'person', 0.05, 2, 0.9),
    (onboard_module, 'person', 0.1, 6, 0.6),
    (onboard_module, 'person', 0.1, 6, 0.3),
    (onboard_cable, 'person', 1, 48, 0.9),
    (onboard_cable, 'person', 1, 48, 0.8),
    (onboard_cable, 'sync-bot', 2, 72, 0.5),
    (onboard_ip, 'ipam-bot', 1, 72, 0.8),
    (onboard_activate, 'person', 24, 168, 0.7),
)


def onboarding_events(rng, start, end, tz, count):
    """Events (time, actor, operation, ctx) for count server onboardings."""
    people = [a[0] for a in ACTORS if a[2] == 'person' and a[0] != 'olga']
    events = []
    span = (end - start).total_seconds() - 8 * 86400
    for _ in range(count):
        t0 = start + timedelta(seconds=rng.uniform(0, max(span, 3600)))
        person = rng.choice(people)
        ctx = {}
        events.append((t0, person, onboard_create, ctx))
        for func, actor, lo, hi, p in ONBOARDING:
            if rng.random() < p:
                who = person if actor == 'person' else actor
                events.append((t0 + timedelta(hours=rng.uniform(lo, hi)), who, func, ctx))
    return events


OPERATIONS = {name[3:]: func for name, func in globals().items() if name.startswith('op_')}


def actor_times(rng, start, end, tz, share, hours, weekdays, count):
    """count random times in [start, end) for one actor, inside its active hours."""
    out = []
    days = (end - start).days
    while len(out) < count:
        day = start + timedelta(days=rng.randrange(days))
        local = day.astimezone(tz)
        if weekdays and local.weekday() >= 5 and rng.random() < 0.9:
            continue
        t = datetime.combine(local.date(), datetime.min.time(), tz) + timedelta(
            hours=rng.choice(list(hours)), seconds=rng.randrange(3600),
        )
        if start <= t < end:
            out.append(t)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--days', type=int, default=90)
    ap.add_argument('--sets', type=int, default=20000, help='change sets at scale 1')
    ap.add_argument('--scale', type=float, default=0.25)
    ap.add_argument('--devices', type=int, default=300, help='base devices, created without change records')
    ap.add_argument('--seed', type=int, default=1)
    ap.add_argument('--onboardings', type=int, default=0, help='server onboardings spread over hours and days')
    args = ap.parse_args()

    if not settings.DEBUG and not os.environ.get('GENERATE_ALLOW'):
        print('Refusing to run with DEBUG off. Set GENERATE_ALLOW=1 for a non-debug development instance.')
        sys.exit(2)

    rng = random.Random(args.seed)
    tz = ZoneInfo(settings.TIME_ZONE)
    end = datetime.now(tz)
    start = end - timedelta(days=args.days)
    total = int(args.sets * args.scale)

    w = World(rng)
    t0 = clock.monotonic()
    w.setup(args.devices)
    print(f'setup: {clock.monotonic() - t0:.0f} s', file=sys.stderr)

    group, _ = Group.objects.get_or_create(name=AUTOMATION_GROUP)
    users = {}
    for name, share, kind, hours, weekdays in ACTORS:
        u, _ = User.objects.get_or_create(username=name)
        if kind == 'bot':
            u.groups.add(group)
        users[name] = u

    events = []
    for name, share, kind, hours, weekdays in ACTORS:
        a_start = start if name != 'olga' else start
        a_end = end if name != 'olga' else start + timedelta(days=args.days // 3)
        for t in actor_times(rng, a_start, a_end, tz, share, hours, weekdays, int(total * share)):
            events.append((t, name, None, None))
    events.extend(onboarding_events(rng, start, end, tz, args.onboardings))
    events.sort(key=lambda e: e[0])

    times = []  # (request_id, time)
    done = Counter()
    for i, (t, name, func, ctx) in enumerate(events):
        if func is None:
            kind = 'person' if name not in OPS else name
            ops, weights = zip(*OPS[kind])
            op = rng.choices(ops, weights)[0]
        else:
            op = func.__name__
            if func is not onboard_create and 'device' not in ctx:
                continue
        msg = 'synthetic change' if rng.random() < MESSAGE_SHARE else ''
        rid = uuid.uuid4()
        request = NetBoxFakeRequest({
            'META': {}, 'COOKIES': {}, 'POST': {}, 'GET': {}, 'FILES': {},
            'user': users[name], 'method': 'POST', 'path': '', 'id': rid,
        })
        try:
            with transaction.atomic(), event_tracking(request):
                if func is None:
                    OPERATIONS[op](w, msg)
                else:
                    func(w, msg, ctx)
        except Exception as e:  # an invalid random pick; skip it
            done[f'error {op}: {type(e).__name__}'] += 1
            continue
        times.append((rid, t))
        done[op] += 1
        if i % 500 == 0:
            print(f'{i}/{len(events)} {clock.monotonic() - t0:.0f} s', file=sys.stderr)

    # Move each change set to its time; records of one set are 1 ms apart.
    with connection.cursor() as cursor:
        for j in range(0, len(times), 1000):
            chunk = times[j:j + 1000]
            values = ','.join(['(%s::uuid, %s::timestamptz)'] * len(chunk))
            params = [p for rid, t in chunk for p in (str(rid), t.isoformat())]
            cursor.execute(
                f'UPDATE core_objectchange oc SET time = v.t + (oc.id - m.min_id) * interval \'1 millisecond\' '
                f'FROM (VALUES {values}) AS v(rid, t), '
                f'(SELECT request_id, min(id) AS min_id FROM core_objectchange GROUP BY request_id) AS m '
                f'WHERE oc.request_id = v.rid AND m.request_id = v.rid',
                params,
            )

    # A deleted user: their records keep user_name, user becomes null.
    users['olga'].delete()

    sets = ObjectChange.objects.filter(request_id__in=[rid for rid, _ in times])
    print(f'done in {clock.monotonic() - t0:.0f} s: {len(times)} change sets, {sets.count()} records',
          file=sys.stderr)
    for k, v in sorted(done.items()):
        print(f'  {k}: {v}', file=sys.stderr)
    cable_ct = ContentType.objects.get_for_model(Cable)
    print(f'  cable records: {sets.filter(changed_object_type=cable_ct).count()}', file=sys.stderr)


if __name__ == '__main__':
    main()
