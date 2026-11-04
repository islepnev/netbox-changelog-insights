"""Scope: the site and the tenant of a change record (design decision 12).

The site of a record, first found of:

1. the record is about a site: the site itself;
2. the "site" field of its data (devices, racks, locations, VLANs, virtual machines);
3. the "_site" field of its data: the site that NetBox caches on device components,
   cable terminations, prefixes and clusters;
4. its device or virtual machine: the related object (components), the "device" or
   "virtual_machine" field of its data (modules, inventory assets), the "_device" field
   (cable terminations), or the interface it is assigned to (IP and MAC addresses,
   FHRP assignments). The site of that device or virtual machine is read from the
   current object, else from its latest change record (a deleted device);
5. a cable: the "_site" of its terminations in the same change set, else of its current
   terminations.

The tenant of a record: the tenant itself; the "tenant" field of its data; else the
tenant of its device or virtual machine (step 4). An object with no tenant of its own,
for example an interface or an IP address without a tenant, takes the tenant of its device.

The data is the post-change data, else the pre-change data (a deletion). Both values are
SQL expressions over the outer "core_objectchange" row, so that filters and groups stay
in the database.
"""
from django.contrib.contenttypes.models import ContentType
from django.db.models import BigIntegerField
from django.db.models.expressions import RawSQL

from core.models import ObjectChange

__all__ = (
    'SCOPES',
    'root_scopes',
    'scope_expression',
    'scope_sql',
)

SCOPES = ('site', 'tenant')

_TABLE = 'core_objectchange'
_HOST_APPS = ('dcim', 'netbox_inventory')
_TYPES = {
    'site': ('dcim', 'site'),
    'tenant': ('tenancy', 'tenant'),
    'device': ('dcim', 'device'),
    'vm': ('virtualization', 'virtualmachine'),
    'interface': ('dcim', 'interface'),
    'vminterface': ('virtualization', 'vminterface'),
    'cable': ('dcim', 'cable'),
    'termination': ('dcim', 'cabletermination'),
}


def _num(key, table=_TABLE):
    """SQL: a number field of the data (post-change, else pre-change) as bigint; else NULL."""
    value = f'COALESCE("{table}"."postchange_data" -> \'{key}\', "{table}"."prechange_data" -> \'{key}\')'
    return f"(CASE WHEN jsonb_typeof({value}) = 'number' THEN ({value} #>> '{{}}')::numeric::bigint END)"


def _type_ids():
    ids = {}
    for name, (app, model) in _TYPES.items():
        ct = ContentType.objects.filter(app_label=app, model=model).values_list('pk', flat=True).first()
        ids[name] = ct or 0
    ids['host_apps'] = list(ContentType.objects.filter(app_label__in=_HOST_APPS).values_list('pk', flat=True)) or [0]
    ids['vm_apps'] = list(ContentType.objects.filter(app_label='virtualization').values_list('pk', flat=True)) or [0]
    return ids


def _host_value(field, table, ct, id_sql):
    """
    SQL: field ("site" or "tenant") of the device or virtual machine with id id_sql: the
    current object, else its latest change record when the object does not exist any more.
    """
    latest = (
        f'(SELECT {_num(field, "h")} FROM {_TABLE} AS h WHERE h.changed_object_type_id = {ct} '
        f'AND h.changed_object_id = x.id ORDER BY h.id DESC LIMIT 1)'
    )
    return (
        f'(SELECT CASE WHEN o.id IS NOT NULL THEN o.{field}_id ELSE {latest} END '
        f'FROM (SELECT {id_sql} AS id) AS x LEFT JOIN {table} AS o ON o.id = x.id WHERE x.id IS NOT NULL)'
    )


def scope_sql(scope):
    """SQL expression: the site or tenant id of the outer core_objectchange row, or NULL."""
    from dcim.models import Device, Interface
    from virtualization.models import VirtualMachine, VMInterface
    if scope not in SCOPES:
        raise ValueError(scope)
    t = _type_ids()
    host_apps = ', '.join(str(i) for i in t['host_apps'])
    vm_apps = ', '.join(str(i) for i in t['vm_apps'])
    rel_type, rel_id = f'"{_TABLE}"."related_object_type_id"', f'"{_TABLE}"."related_object_id"'
    own_type, own_id = f'"{_TABLE}"."changed_object_type_id"', f'"{_TABLE}"."changed_object_id"'
    assigned_type, assigned_id = _num('assigned_object_type'), _num('assigned_object_id')
    device = (
        f'COALESCE('
        f'CASE WHEN {rel_type} = {t["device"]} THEN {rel_id} END, '
        f'CASE WHEN {own_type} IN ({host_apps}) AND {own_type} <> {t["device"]} THEN {_num("device")} END, '
        f'{_num("_device")}, '
        f'CASE WHEN {assigned_type} = {t["interface"]} THEN '
        f'(SELECT i.device_id FROM {Interface._meta.db_table} AS i WHERE i.id = {assigned_id}) END)'
    )
    vm = (
        f'COALESCE('
        f'CASE WHEN {rel_type} = {t["vm"]} THEN {rel_id} END, '
        f'CASE WHEN {own_type} IN ({vm_apps}) AND {own_type} <> {t["vm"]} THEN {_num("virtual_machine")} END, '
        f'CASE WHEN {assigned_type} = {t["vminterface"]} THEN '
        f'(SELECT i.virtual_machine_id FROM {VMInterface._meta.db_table} AS i WHERE i.id = {assigned_id}) END)'
    )
    parts = [
        f'CASE WHEN {own_type} = {t[scope]} THEN {own_id} END',
        _num(scope),
    ]
    if scope == 'site':
        parts.append(_num('_site'))
    parts += [
        _host_value(scope, Device._meta.db_table, t['device'], device),
        _host_value(scope, VirtualMachine._meta.db_table, t['vm'], vm),
    ]
    if scope == 'site':
        from dcim.models import CableTermination
        parts.append(
            f'CASE WHEN {own_type} = {t["cable"]} THEN COALESCE('
            f'(SELECT {_num("_site", "c")} FROM {_TABLE} AS c WHERE c.request_id = "{_TABLE}"."request_id" '
            f'AND c.changed_object_type_id = {t["termination"]} AND {_num("cable", "c")} = {own_id} '
            f'AND {_num("_site", "c")} IS NOT NULL ORDER BY c.id LIMIT 1), '
            f'(SELECT ct._site_id FROM {CableTermination._meta.db_table} AS ct WHERE ct.cable_id = {own_id} '
            f'AND ct._site_id IS NOT NULL ORDER BY ct.id LIMIT 1)) END'
        )
    return f'COALESCE({", ".join(parts)})'


def scope_expression(scope):
    """The site or tenant id of a record, as an expression for QuerySet.alias() or annotate()."""
    return RawSQL(scope_sql(scope), (), output_field=BigIntegerField())


def root_scopes(roots, scope):
    """
    {(type id, object id): site or tenant id} for chain roots (devices and virtual
    machines): the current object, else its latest change record.
    """
    from dcim.models import Device
    from virtualization.models import VirtualMachine
    out = {}
    for model in (Device, VirtualMachine):
        ct = ContentType.objects.get_for_model(model)
        ids = {i for t, i in roots if t == ct.pk}
        if not ids:
            continue
        found = dict(model.objects.filter(pk__in=ids).values_list('pk', f'{scope}_id'))
        missing = ids - set(found)
        if missing:
            latest = ObjectChange.objects.filter(changed_object_type=ct, changed_object_id__in=missing).order_by(
                'changed_object_id', '-pk').distinct('changed_object_id')
            for rec in latest:
                data = rec.postchange_data or rec.prechange_data or {}
                value = data.get(scope)
                found[rec.changed_object_id] = value if isinstance(value, int) else None
        for i in ids:
            out[(ct.pk, i)] = found.get(i)
    return out
