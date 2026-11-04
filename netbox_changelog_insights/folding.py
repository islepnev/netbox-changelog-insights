"""Fold side-effect records into their cause within a change set (design decision 3).

A change set is the group of ObjectChange records that share a request_id. A record
is folded when one of these rules applies, in this order:

1. repeat: another record in the set is the main record of the same object: its
   create, else its delete, else its first update. NetBox writes a create and then
   an update for a new cable in the same request, and an update before the delete
   of a device.
2. cable: the record is for a cable termination and a cable record is in the set.
3. cable fields: the record is an update that changes only cable fields
   (CABLE_FIELDS) and a cable record is in the set.
4. primary IP: the record is an update of a device or virtual machine that changes
   only primary IP fields (PRIMARY_IP_FIELDS), and the IP address it points to (before
   or after) has a record in the set. "Make this the primary IP" in the IP address
   form does this.
5. related: the related object (for example the device of an interface) has a
   record in the set; the parent is the main record of that object.

A record that no rule folds is a primary record. Counts and rankings use primary
records. The folded records stay available under their parent.
"""
from django.contrib.contenttypes.models import ContentType
from django.db.models import (
    BigIntegerField, BooleanField, Case, Exists, F, IntegerField, OuterRef, Q, Subquery, Value, When,
)
from django.db.models.expressions import RawSQL
from django.db.models.functions import Coalesce, NullIf

__all__ = (
    'CABLE_FIELDS',
    'DIFF_IGNORED_FIELDS',
    'FOLD_CABLE',
    'FOLD_CABLE_FIELDS',
    'FOLD_PRIMARY_IP',
    'FOLD_RELATED',
    'PRIMARY_IP_FIELDS',
    'FOLD_REPEAT',
    'annotate_fold',
    'fold_rule',
    'fold_tree',
)

# Fields that CabledObjectModel.set_cable_termination() and clear_cable_termination() write.
CABLE_FIELDS = ('cable', 'cable_end', 'cable_connector', 'cable_positions')

# Fields of a device or virtual machine that point to its primary IP addresses.
PRIMARY_IP_FIELDS = ('primary_ip4', 'primary_ip6', 'oob_ip')
PRIMARY_IP_MODELS = (('dcim', 'device'), ('virtualization', 'virtualmachine'))

# Fields that ObjectChange.diff() ignores; keys that start with "_" are ignored too.
DIFF_IGNORED_FIELDS = ('created', 'last_updated', 'level', 'lft', 'rght', 'tree_id')

FOLD_REPEAT = 'repeat'
FOLD_CABLE = 'cable'
FOLD_CABLE_FIELDS = 'cable fields'
FOLD_PRIMARY_IP = 'primary ip'
FOLD_RELATED = 'related'

_TABLE = 'core_objectchange'


def _only_fields_update_sql(fields):
    """SQL: true if the record changes at least one of fields and no other field."""
    pre = f'COALESCE("{_TABLE}"."prechange_data", \'{{}}\'::jsonb)'
    post = f'COALESCE("{_TABLE}"."postchange_data", \'{{}}\'::jsonb)'
    cable = ', '.join(f"'{f}'" for f in fields)
    ignored = ', '.join(f"'{f}'" for f in DIFF_IGNORED_FIELDS)
    changed = f'({pre} -> k.key) IS DISTINCT FROM ({post} -> k.key)'
    keys = f'jsonb_each({pre} || {post}) AS k'
    # One of fields changed: plain comparisons, so that the key scan below runs only then
    # (the key scan is the cost of this test: 90 days of synthetic data, 2.6 s to 1.2 s).
    any_changed = ' OR '.join(f"({pre} -> '{f}') IS DISTINCT FROM ({post} -> '{f}')" for f in fields)
    return (
        f'(({any_changed}) '
        f'AND NOT EXISTS (SELECT 1 FROM {keys} WHERE k.key NOT IN ({cable}) AND k.key NOT IN ({ignored}) '
        f"AND left(k.key, 1) <> '_' AND {changed}))"
    )


def _data_ref_sql(field, data='postchange_data'):
    """SQL: the id in a field of the record data, as bigint."""
    return f'("{_TABLE}"."{data}" ->> \'{field}\')::bigint'


def _cable_ref_sql():
    """SQL: the cable id in the record data, after the change or else before it."""
    return f"COALESCE({_data_ref_sql('cable')}, {_data_ref_sql('cable', 'prechange_data')})"


def annotate_fold(queryset, scope):
    """
    Annotate each record of queryset with fold_parent: the id of the record it folds
    into, or None for a primary record. scope is the queryset that holds the other
    records of each change set; pass the permission-restricted ObjectChange queryset,
    so that a record never folds into a record the user cannot see.
    """
    cable_type = ContentType.objects.get_by_natural_key('dcim', 'cable')
    termination_type = ContentType.objects.get_by_natural_key('dcim', 'cabletermination')
    ip_type = ContentType.objects.get_by_natural_key('ipam', 'ipaddress')
    host_types = [ContentType.objects.get_by_natural_key(*m).pk for m in PRIMARY_IP_MODELS]

    # The main record of an object in a set: its create, else its delete, else its first update.
    rank = Case(
        When(action='create', then=Value(0)),
        When(action='delete', then=Value(1)),
        default=Value(2),
        output_field=IntegerField(),
    )
    main_record = scope.filter(request_id=OuterRef('request_id')).annotate(_rank=rank).order_by('_rank', 'pk')
    repeat = main_record.filter(
        changed_object_type_id=OuterRef('changed_object_type_id'),
        changed_object_id=OuterRef('changed_object_id'),
    ).values('pk')[:1]
    cables = main_record.filter(changed_object_type_id=cable_type.pk).values('pk')
    cable_parent = Coalesce(
        Subquery(cables.filter(changed_object_id=OuterRef('_fold_cable_ref'))[:1]),
        Subquery(cables[:1]),
    )
    ip_refs = {
        f'_fold_ip_{data}_{field}': RawSQL(_data_ref_sql(field, f'{data}change_data'), (), output_field=BigIntegerField())
        for field in PRIMARY_IP_FIELDS for data in ('post', 'pre')
    }
    ip_match = Q()
    for name in ip_refs:
        ip_match |= Q(changed_object_id=OuterRef(name))
    ip_parent = main_record.filter(ip_match, changed_object_type_id=ip_type.pk).values('pk')[:1]
    related = main_record.filter(
        changed_object_type_id=OuterRef('related_object_type_id'),
        changed_object_id=OuterRef('related_object_id'),
    ).exclude(pk=OuterRef('pk')).values('pk')[:1]

    return queryset.annotate(
        _fold_cable_ref=RawSQL(_cable_ref_sql(), (), output_field=BigIntegerField()),
        _fold_cable_only=RawSQL(_only_fields_update_sql(CABLE_FIELDS), (), output_field=BooleanField()),
        _fold_primary_ip_only=RawSQL(_only_fields_update_sql(PRIMARY_IP_FIELDS), (), output_field=BooleanField()),
        **ip_refs,
    ).annotate(
        fold_parent=Coalesce(
            NullIf(Subquery(repeat), F('pk')),
            Case(
                When(changed_object_type_id=termination_type.pk, then=cable_parent),
                # Check for a cable in the set before the field diff, which reads the jsonb data.
                When(Q(action='update') & Exists(cables) & Q(_fold_cable_only=True), then=cable_parent),
                default=None,
                output_field=BigIntegerField(),
            ),
            Case(
                When(
                    Q(action='update') & Q(changed_object_type_id__in=host_types) & Q(_fold_primary_ip_only=True),
                    then=Subquery(ip_parent),
                ),
                default=None,
                output_field=BigIntegerField(),
            ),
            Case(
                When(related_object_id__isnull=False, then=Subquery(related)),
                default=None,
                output_field=BigIntegerField(),
            ),
            output_field=BigIntegerField(),
        )
    )


def fold_rule(record, parent):
    """Name the rule that folded record into parent (both are ObjectChange instances)."""
    if (record.changed_object_type_id, record.changed_object_id) == \
            (parent.changed_object_type_id, parent.changed_object_id):
        return FOLD_REPEAT
    if parent.changed_object_type.model == 'cable' and parent.changed_object_type.app_label == 'dcim':
        if record.changed_object_type.model == 'cabletermination':
            return FOLD_CABLE
        if record.action == 'update':
            return FOLD_CABLE_FIELDS
    if (parent.changed_object_type.app_label, parent.changed_object_type.model) == ('ipam', 'ipaddress') \
            and record.action == 'update' and record.related_object_id is None:
        return FOLD_PRIMARY_IP
    return FOLD_RELATED


def fold_tree(records):
    """
    Arrange the annotated records of one change set as a tree.

    Return a list of (primary record, [(folded record, rule, depth), ...]) in record
    order. A folded record is placed under the primary record at the root of its
    chain of parents (a termination folds into an interface update, which folds into
    the cable).
    """
    by_id = {r.pk: r for r in records}
    children = {}
    for r in records:
        if r.fold_parent is not None and r.fold_parent in by_id:
            children.setdefault(r.fold_parent, []).append(r)
    roots = [r for r in records if r.fold_parent is None or r.fold_parent not in by_id]

    tree = []
    seen = set()
    for root in roots:
        folded = []
        stack = [(c, 1) for c in reversed(children.get(root.pk, []))]
        seen.add(root.pk)
        while stack:
            node, depth = stack.pop()
            if node.pk in seen:
                continue
            seen.add(node.pk)
            folded.append((node, fold_rule(node, by_id[node.fold_parent]), depth))
            stack.extend((c, depth + 1) for c in reversed(children.get(node.pk, [])))
        tree.append((root, folded))
    # Records in a cycle of parents have no root; show them as primary.
    for r in records:
        if r.pk not in seen:
            tree.append((r, []))
            seen.add(r.pk)
    return tree
