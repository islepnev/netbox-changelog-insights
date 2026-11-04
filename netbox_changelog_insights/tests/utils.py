import uuid
from contextlib import contextmanager

from core.models import ObjectChange
from dcim.models import Device, DeviceRole, DeviceType, InterfaceTemplate, Manufacturer, Site
from netbox.context_managers import event_tracking
from utilities.request import NetBoxFakeRequest


@contextmanager
def change_set(user):
    """Make changes as one request of user; yields the request id."""
    rid = uuid.uuid4()
    request = NetBoxFakeRequest({
        'META': {}, 'COOKIES': {}, 'POST': {}, 'GET': {}, 'FILES': {},
        'user': user, 'method': 'POST', 'path': '', 'id': rid,
    })
    with event_tracking(request):
        yield rid


def records(rid):
    return ObjectChange.objects.filter(request_id=rid)


def make_inventory(interfaces=3):
    """A site, a device type with interfaces and two devices, without change records."""
    site = Site.objects.create(name='Site 1', slug='site-1')
    mf = Manufacturer.objects.create(name='M', slug='m')
    role = DeviceRole.objects.create(name='R', slug='r')
    dt = DeviceType.objects.create(manufacturer=mf, model='T', slug='t')
    for i in range(1, interfaces + 1):
        InterfaceTemplate.objects.create(device_type=dt, name=f'eth{i}', type='1000base-t')
    d1 = Device.objects.create(name='d1', device_type=dt, role=role, site=site)
    d2 = Device.objects.create(name='d2', device_type=dt, role=role, site=site)
    return {'site': site, 'dt': dt, 'role': role, 'mf': mf, 'd1': d1, 'd2': d2}
