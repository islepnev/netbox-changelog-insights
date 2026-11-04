"""Chains: change sets about one device or virtual machine over time (design decision 11).

A chain is a series of change sets that touch one root (a device or a virtual machine),
for example the onboarding of a server: the device, its modules and ports, cables,
rack position, assets and IP addresses, over hours or days. Rules:

- The root of a record: the device or VM itself; else its related object (components,
  contact assignments, images); else the "device" or "virtual_machine" field of its
  data (modules, inventory assets); else the component it is attached to (cable
  terminations, IP and MAC addresses, FHRP assignments); a cable has the roots of its
  terminations. A change set has the roots of its records.
- Change sets on one root join a chain when the gap to the previous one is up to
  chain_gap (default 7 days). A gap longer than automation_gap (default 24 hours)
  joins only if a person's change set follows within chain_gap: automation work
  between a person's steps joins the chain, but periodic syncs of automation
  accounts after the work do not stretch it.
- A change set with several roots (a cable between two devices) belongs to the chain
  of each root; chains are not joined through it.

tools/chain_audit.py measures these rules on a changelog.
"""
from collections import defaultdict
from dataclasses import dataclass, field

from django.contrib.contenttypes.models import ContentType
from django.db.models.fields.json import KT
from django.db.models.functions import Coalesce


__all__ = (
    'DEVICE',
    'STAGES',
    'VM',
    'Chain',
    'build_chains',
)

DEVICE = ('dcim', 'device')
VM = ('virtualization', 'virtualmachine')
ROOT_MODELS = (DEVICE, VM)

# Stages, in display order: (key, label).
STAGES = (
    ('create', 'Created'),
    ('components', 'Components'),
    ('modules', 'Modules'),
    ('cabling', 'Cabling'),
    ('metadata', 'Device fields'),
    ('component edits', 'Component edits'),
    ('assets', 'Assets'),
    ('ip', 'IP addresses'),
    ('contacts', 'Contacts'),
    ('delete', 'Deleted'),
    ('other', 'Other'),
)
STAGE_ORDER = {key: i for i, (key, _) in enumerate(STAGES)}

# Data keys read from each record (post-change, else pre-change).
_DATA_KEYS = (
    'device', 'virtual_machine', 'cable', '_device',
    'assigned_object_type', 'assigned_object_id', 'object_type', 'object_id',
    'termination_type', 'termination_id', 'interface_type', 'interface_id',
)
_ATTACHMENTS = (
    ('assigned_object_type', 'assigned_object_id'),
    ('termination_type', 'termination_id'),
    ('interface_type', 'interface_id'),
    ('object_type', 'object_id'),
)


@dataclass
class Chain:
    root: tuple                     # (content type id, object id)
    sets: list = field(default_factory=list)   # change set dicts, in time order

    @property
    def start(self):
        return self.sets[0]['time']

    @property
    def end(self):
        return self.sets[-1]['time']

    @property
    def duration(self):
        return self.end - self.start

    @property
    def onboarding(self):
        return 'create' in self.sets[0]['stages'].get(self.root, ())

    @property
    def stages(self):
        """Stage sequence, consecutive repeats removed."""
        out = []
        for cs in self.sets:
            for s in sorted(cs['stages'].get(self.root, ()), key=STAGE_ORDER.get):
                if not out or out[-1] != s:
                    out.append(s)
        return out

    @property
    def records(self):
        return sum(len(cs['records'].get(self.root, ())) for cs in self.sets)

    @property
    def users(self):
        seen = []
        for cs in self.sets:
            if cs['user'] not in seen:
                seen.append(cs['user'])
        return seen


class RootResolver:
    """Find the roots (device or VM) of change records, from the changelog itself."""

    def __init__(self, scope):
        self.scope = scope
        self.ct = {ct.pk: (ct.app_label, ct.model) for ct in ContentType.objects.all()}
        self.ct_id = {v: k for k, v in self.ct.items()}
        self.root_types = {self.ct_id[m] for m in ROOT_MODELS if m in self.ct_id}
        self.parent = {}            # (type id, object id) -> root
        self.cable_roots = defaultdict(set)

    def own_root(self, row):
        """Root from the record alone: itself, its related object, or a device/VM field."""
        t = row['changed_object_type_id']
        if t in self.root_types:
            return (t, row['changed_object_id'])
        if row['related_object_type_id'] in self.root_types:
            return (row['related_object_type_id'], row['related_object_id'])
        app = self.ct.get(t, ('', ''))[0]
        if isinstance(row['d_device'], int) and app in ('dcim', 'netbox_inventory'):
            return (self.ct_id[DEVICE], row['d_device'])
        if isinstance(row['d_virtual_machine'], int) and app == 'virtualization':
            return (self.ct_id[VM], row['d_virtual_machine'])
        if isinstance(row['d__device'], int):
            return (self.ct_id[DEVICE], row['d__device'])
        return None

    @staticmethod
    def attachment(row):
        """(type id, object id) of the component a record is attached to, if any."""
        if row['related_object_type_id'] and row['related_object_id']:
            return (row['related_object_type_id'], row['related_object_id'])
        for type_key, id_key in _ATTACHMENTS:
            t, i = row[f'd_{type_key}'], row[f'd_{id_key}']
            if isinstance(t, int) and isinstance(i, int):
                return (t, i)
        return None

    def learn(self, rows):
        """Learn component -> root and cable -> roots from the given records."""
        for row in rows:
            root = self.own_root(row)
            if root and row['changed_object_type_id'] not in self.root_types:
                self.parent[(row['changed_object_type_id'], row['changed_object_id'])] = root
            if self.ct.get(row['changed_object_type_id']) == ('dcim', 'cabletermination'):
                if isinstance(row['d_cable'], int) and isinstance(row['d__device'], int):
                    self.cable_roots[row['d_cable']].add((self.ct_id[DEVICE], row['d__device']))

    def learn_missing(self, refs, cables):
        """Find components and cables outside the given records in the rest of the changelog."""
        refs = [r for r in refs if r not in self.parent]
        by_type = defaultdict(set)
        for t, i in refs:
            by_type[t].add(i)
        for t, ids in by_type.items():
            rows = self.scope.filter(changed_object_type_id=t, changed_object_id__in=ids)
            self.learn(_rows(rows))
        cables = [c for c in cables if c not in self.cable_roots]
        term = self.ct_id.get(('dcim', 'cabletermination'))
        if cables and term:
            rows = self.scope.filter(changed_object_type_id=term).annotate(
                d_cable_any=Coalesce(KT('postchange_data__cable'), KT('prechange_data__cable')),
            ).filter(d_cable_any__in=[str(c) for c in cables])
            self.learn(_rows(rows))

    def roots(self, row):
        t = row['changed_object_type_id']
        if self.ct.get(t) == ('dcim', 'cable'):
            return set(self.cable_roots.get(row['changed_object_id'], ()))
        root = self.own_root(row)
        if root:
            return {root}
        ref = self.attachment(row)
        if ref:
            if ref[0] in self.root_types:
                return {ref}
            if ref in self.parent:
                return {self.parent[ref]}
        return set()


def _rows(qs):
    """Narrow rows with the data keys that root resolution needs, as Python values."""
    annotations = {
        f'd_{k}': Coalesce(KT(f'postchange_data__{k}'), KT(f'prechange_data__{k}')) for k in _DATA_KEYS
    }
    rows = qs.annotate(**annotations).values(
        'pk', 'request_id', 'time', 'user_id', 'user_name', 'action',
        'changed_object_type_id', 'changed_object_id', 'related_object_type_id', 'related_object_id',
        *annotations,
    ).order_by()
    out = []
    for row in rows:
        for k in _DATA_KEYS:
            v = row[f'd_{k}']
            row[f'd_{k}'] = int(v) if isinstance(v, str) and v.isdigit() else None
        out.append(row)
    return out


def stage_of(row, root, ct):
    """Stage of a primary record within the chain of root."""
    app_model = ct.get(row['changed_object_type_id'], ('', ''))
    action = row['action']
    if (row['changed_object_type_id'], row['changed_object_id']) == root:
        return {'create': 'create', 'delete': 'delete'}.get(action, 'metadata')
    app, model = app_model
    if (app, model) in (('dcim', 'cable'), ('dcim', 'cabletermination')):
        return 'cabling'
    if (app, model) == ('dcim', 'module'):
        return 'modules'
    if app == 'netbox_inventory':
        return 'assets'
    if (app, model) in (('ipam', 'ipaddress'), ('dcim', 'macaddress'), ('ipam', 'fhrpgroupassignment')):
        return 'ip'
    if (app, model) == ('tenancy', 'contactassignment'):
        return 'contacts'
    if app in ('dcim', 'virtualization'):
        return 'components' if action in ('create', 'delete') else 'component edits'
    return 'other'


def build_chains(records, primary_ids, scope, automation_ids, chain_gap, automation_gap):
    """
    Build chains from records (the period plus a lead-in of chain_gap, all records, folded
    or not) and primary_ids (ids of the primary records). Return (chains, resolver).
    """
    resolver = RootResolver(scope)
    rows = _rows(records)
    resolver.learn(rows)
    refs = {resolver.attachment(r) for r in rows} - {None}
    cables = {r['changed_object_id'] for r in rows if resolver.ct.get(r['changed_object_type_id']) == ('dcim', 'cable')}
    resolver.learn_missing(refs, cables)

    sets = {}
    for row in rows:
        cs = sets.setdefault(row['request_id'], {
            'request_id': row['request_id'], 'time': row['time'], 'user_id': row['user_id'],
            'user': row['user_name'], 'roots': set(), 'records': defaultdict(list), 'stages': defaultdict(set),
        })
        cs['time'] = min(cs['time'], row['time'])
        roots = resolver.roots(row)
        cs['roots'] |= roots
        if row['pk'] in primary_ids:
            for root in roots:
                cs['records'][root].append(row['pk'])
                cs['stages'][root].add(stage_of(row, root, resolver.ct))

    timeline = defaultdict(list)
    for cs in sets.values():
        for root in cs['records']:
            timeline[root].append(cs)
    chains = []
    for root, items in timeline.items():
        items.sort(key=lambda cs: cs['time'])
        person_times = [cs['time'] for cs in items if cs['user_id'] not in automation_ids]
        chain = Chain(root=root, sets=[items[0]])
        for cs in items[1:]:
            if _joins(chain.end, cs, person_times, automation_ids, chain_gap, automation_gap):
                chain.sets.append(cs)
            else:
                chains.append(chain)
                chain = Chain(root=root, sets=[cs])
        chains.append(chain)
    return chains, resolver


def _joins(end, cs, person_times, automation_ids, chain_gap, automation_gap):
    """
    True if change set cs continues a chain that ends at end. Gaps up to automation_gap
    join; longer gaps up to chain_gap join only if a person's change set falls within
    chain_gap of end, at or after cs: automation work joins a chain but never extends it.
    """
    gap = cs['time'] - end
    if gap <= automation_gap:
        return True
    if gap > chain_gap:
        return False
    return any(cs['time'] <= t <= end + chain_gap for t in person_times)
