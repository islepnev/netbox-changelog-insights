#!/usr/bin/env python3
"""Measure how change sets form logical chains over time, on a NetBox changelog.

A chain is a series of change sets about one device or virtual machine (the root),
with gaps between them no longer than a threshold: for example the onboarding of a
server, from the device create through modules, cables, serial number, inventory
assets and IP addresses. The script tells how reliably the root of a change set can
be found, how long the gaps are, and which stage sequences occur.

Read only: GET requests only. The report holds counts, object type names and field
names; no object names, user names or values.

Usage (fold_audit.py must be in the same directory):
    NETBOX_URL=https://netbox.example.com NETBOX_TOKEN=... \
        python3 chain_audit.py --days 90 [--automation-group automation] > chain_audit.md
"""
import argparse
import os
import statistics
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fold_audit import Api, bucket, diff_keys, fetch_object_types, fetch_records, fold, table  # noqa: E402

PLUGIN_RULES = ('repeat', 'cable', 'cablef', 'primaryip', 'related')
DEVICE, VM = 'device', 'vm'

# Types whose data names the parent device or virtual machine in a field.
DEVICE_FIELD_PREFIXES = ('dcim.', 'netbox_inventory.')
COMPONENT_ENDPOINTS = {
    'dcim.interface': 'dcim/interfaces/',
    'dcim.frontport': 'dcim/front-ports/',
    'dcim.rearport': 'dcim/rear-ports/',
    'dcim.consoleport': 'dcim/console-ports/',
    'dcim.consoleserverport': 'dcim/console-server-ports/',
    'dcim.powerport': 'dcim/power-ports/',
    'dcim.poweroutlet': 'dcim/power-outlets/',
    'virtualization.vminterface': 'virtualization/interfaces/',
}

STAGES = (
    ('create', 'device or VM created'),
    ('components', 'components created or deleted'),
    ('modules', 'modules installed or removed'),
    ('cabling', 'cables created, changed or removed'),
    ('metadata', 'device or VM fields changed'),
    ('component edits', 'component fields changed'),
    ('assets', 'inventory assets'),
    ('ip', 'IP or MAC addresses'),
    ('contacts', 'contact assignments'),
    ('delete', 'device or VM deleted'),
    ('other', 'other'),
)

GAP_BUCKETS = (
    (60, '<1 min'), (600, '1-10 min'), (3600, '10-60 min'), (8 * 3600, '1-8 h'),
    (24 * 3600, '8-24 h'), (3 * 86400, '1-3 d'), (7 * 86400, '3-7 d'), (30 * 86400, '7-30 d'),
)
THRESHOLDS = ((3600, '1 h'), (8 * 3600, '8 h'), (24 * 3600, '24 h'), (3 * 86400, '3 d'), (7 * 86400, '7 d'))


def gap_bucket(seconds):
    for limit, label in GAP_BUCKETS:
        if seconds < limit:
            return label
    return '>30 d'


def duration_label(seconds):
    if seconds < 3600:
        return f'{seconds / 60:.0f} min'
    if seconds < 86400:
        return f'{seconds / 3600:.1f} h'
    return f'{seconds / 86400:.1f} d'


def ts(rec):
    return datetime.fromisoformat(rec['time']).timestamp()


class Resolver:
    """Find the device or virtual machine that a record belongs to."""

    def __init__(self, api, records, ct_by_id):
        self.api = api
        self.ct_by_id = ct_by_id
        self.parent = {}        # (type, id) -> root, from record data
        self.cable_roots = defaultdict(set)
        self.lookups = 0
        self.unresolved = Counter()
        for r in records:
            for data in (r['prechange_data'] or {}, r['postchange_data'] or {}):
                root = self._own_root(r['changed_object_type'], data)
                if root:
                    self.parent[(r['changed_object_type'], r['changed_object_id'])] = root

    @staticmethod
    def _own_root(t, data):
        if t.startswith(DEVICE_FIELD_PREFIXES) and isinstance(data.get('device'), int):
            return (DEVICE, data['device'])
        if t.startswith('virtualization.') and isinstance(data.get('virtual_machine'), int):
            return (VM, data['virtual_machine'])
        return None

    def lookup_missing(self, records):
        """
        Ask the API for components and cables that the records refer to but do not
        describe. The REST API leaves out data keys that start with "_", so a cable
        termination gives its component, not its cached device.
        """
        want = defaultdict(set)
        cables = set()
        for r in records:
            for data in (r['prechange_data'] or {}, r['postchange_data'] or {}):
                ref = self._assigned_ref(r['changed_object_type'], data)
                if ref and ref not in self.parent and ref[0] in COMPONENT_ENDPOINTS:
                    want[ref[0]].add(ref[1])
        for t, ids in want.items():
            ids = sorted(ids)
            for i in range(0, len(ids), 100):
                page = self.api.get(COMPONENT_ENDPOINTS[t], {'id': ids[i:i + 100], 'limit': 100,
                                                             'fields': 'id,device,virtual_machine'})
                self.lookups += 1
                for obj in page['results']:
                    if obj.get('device'):
                        self.parent[(t, obj['id'])] = (DEVICE, obj['device']['id'])
                    elif obj.get('virtual_machine'):
                        self.parent[(t, obj['id'])] = (VM, obj['virtual_machine']['id'])
        # Cable -> devices, from the terminations in the records, else from the API.
        for r in records:
            if r['changed_object_type'] != 'dcim.cabletermination':
                continue
            for data in (r['prechange_data'] or {}, r['postchange_data'] or {}):
                ref = self._assigned_ref('dcim.cabletermination', data)
                if data.get('cable') and ref in self.parent:
                    self.cable_roots[data['cable']].add(self.parent[ref])
        for r in records:
            if r['changed_object_type'] == 'dcim.cable' and r['changed_object_id'] not in self.cable_roots:
                cables.add(r['changed_object_id'])
        cables = sorted(cables)
        for i in range(0, len(cables), 100):
            page = self.api.get('dcim/cables/', {'id': cables[i:i + 100], 'limit': 100,
                                                 'fields': 'id,a_terminations,b_terminations'})
            self.lookups += 1
            for obj in page['results']:
                for term in (obj.get('a_terminations') or []) + (obj.get('b_terminations') or []):
                    dev = (term.get('object') or {}).get('device')
                    if dev:
                        self.cable_roots[obj['id']].add((DEVICE, dev['id']))

    def _assigned_ref(self, t, data):
        """(type, id) of the object that an IP, MAC, contact, image or cable termination is attached to."""
        for type_field, id_field in (('assigned_object_type', 'assigned_object_id'), ('object_type', 'object_id'),
                                     ('termination_type', 'termination_id')):
            if data.get(type_field) and data.get(id_field):
                rt = self.ct_by_id.get(data[type_field])
                if rt:
                    return (rt, data[id_field])
        return None

    def roots(self, r):
        """Set of roots of one record."""
        t = r['changed_object_type']
        out = set()
        if t == 'dcim.device':
            out.add((DEVICE, r['changed_object_id']))
        elif t == 'virtualization.virtualmachine':
            out.add((VM, r['changed_object_id']))
        elif t == 'dcim.cable':
            out |= self.cable_roots.get(r['changed_object_id'], set())
        else:
            for data in (r['prechange_data'] or {}, r['postchange_data'] or {}):
                root = self._own_root(t, data)
                if root:
                    out.add(root)
                ref = self._assigned_ref(t, data)
                if ref:
                    if ref[0] == 'dcim.device':
                        out.add((DEVICE, ref[1]))
                    elif ref[0] == 'virtualization.virtualmachine':
                        out.add((VM, ref[1]))
                    elif ref in self.parent:
                        out.add(self.parent[ref])
        if not out:
            self.unresolved[t] += 1
        return out


def stage_of(r, root):
    t, a = r['changed_object_type'], r['action']
    own = (t == 'dcim.device' and root[0] == DEVICE) or (t == 'virtualization.virtualmachine' and root[0] == VM)
    if own and r['changed_object_id'] == root[1]:
        return {'create': 'create', 'delete': 'delete'}.get(a, 'metadata')
    if t in ('dcim.cable', 'dcim.cabletermination'):
        return 'cabling'
    if t == 'dcim.module':
        return 'modules'
    if t.startswith('netbox_inventory.'):
        return 'assets'
    if t in ('ipam.ipaddress', 'dcim.macaddress', 'ipam.fhrpgroupassignment'):
        return 'ip'
    if t == 'tenancy.contactassignment':
        return 'contacts'
    if t.startswith('dcim.') or t.startswith('virtualization.'):
        if a in ('create', 'delete'):
            return 'components'
        keys = diff_keys(r)
        if keys and keys <= {'cable', 'cable_end', 'cable_connector', 'cable_positions'}:
            return 'cabling'
        return 'component edits'
    return 'other'


def sequence(stages, limit=7):
    out = []
    for s in stages:
        if not out or out[-1] != s:
            out.append(s)
    if len(out) > limit:
        out = out[:limit] + ['...']
    return ' > '.join(out)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--days', type=int, default=90)
    ap.add_argument('--page', type=int, default=1000)
    ap.add_argument('--automation-group', help='name of the user group of automation accounts')
    ap.add_argument('--insecure', action='store_true', help='do not verify the TLS certificate')
    args = ap.parse_args()

    api = Api(os.environ['NETBOX_URL'], os.environ['NETBOX_TOKEN'], args.insecure)
    started = time.monotonic()
    since = (datetime.now(timezone.utc) - timedelta(days=args.days)).strftime('%Y-%m-%dT%H:%M:%SZ')
    ct_by_id = fetch_object_types(api)
    records = fetch_records(api, since, args.page)
    automation = set()
    if args.automation_group:
        params = {'group': args.automation_group, 'limit': 1000, 'fields': 'id'}
        automation = {u['id'] for u in api.get('users/users/', params)['results']}

    sets = defaultdict(list)
    for r in records:
        sets[r['request_id']].append(r)
    folded = {}
    for cs in sets.values():
        folded.update(fold(cs, ct_by_id, PLUGIN_RULES))

    res = Resolver(api, records, ct_by_id)
    res.lookup_missing(records)

    # Change set -> roots, time, user, primary records per root.
    info = {}
    for rid, cs in sets.items():
        roots = set()
        per_root = defaultdict(list)
        for r in cs:
            rr = res.roots(r)
            roots |= rr
            if r['id'] not in folded:
                for root in rr:
                    per_root[root].append(r)
        info[rid] = {
            'time': min(ts(r) for r in cs), 'user': cs[0]['user'], 'roots': roots, 'per_root': per_root,
            'primary': [r for r in cs if r['id'] not in folded],
        }

    out = [f'# Chain audit, last {args.days} days (since {since})', '']
    out.append(f'Records: {len(records)}, change sets: {len(sets)}, automation users given: {len(automation)}, '
               f'lookups: {res.lookups}.')
    out.append('')

    # 1. Coverage
    out.append('## 1. Root found (device or virtual machine)')
    out.append('')
    with_root = sum(1 for i in info.values() if i['roots'])
    prim = [(r, i) for i in info.values() for r in i['primary']]
    prim_root = sum(1 for r, i in prim if any(r in v for v in i['per_root'].values()))
    out.append(f'- Change sets with a root: {with_root} of {len(info)} ({100 * with_root / max(len(info), 1):.1f}%).')
    out.append(f'- Primary records with a root: {prim_root} of {len(prim)} ({100 * prim_root / max(len(prim), 1):.1f}%).')
    roots_per_set = Counter(bucket(len(i['roots'])) if i['roots'] else '0' for i in info.values())
    out.append('')
    table(out, ['roots in set', 'change sets'],
          [(b, roots_per_set.get(b, 0)) for b in ('0', '1', '2', '3-5', '6-20', '21-100', '101-1000', '>1000')])
    out.append('Primary records without a root, by type:')
    out.append('')
    no_root = Counter(r['changed_object_type'] for r, i in prim if not any(r in v for v in i['per_root'].values()))
    table(out, ['type', 'primary records'], no_root.most_common(15))

    # 2. Gaps between consecutive change sets on one root
    timeline = defaultdict(list)
    for rid, i in info.items():
        for root in i['per_root']:
            timeline[root].append((i['time'], rid))
    for v in timeline.values():
        v.sort()
    gaps = Counter()
    for v in timeline.values():
        for (t1, _), (t2, _) in zip(v, v[1:]):
            gaps[gap_bucket(t2 - t1)] += 1
    out.append('## 2. Gap to the previous change set on the same root')
    out.append('')
    out.append(f'Roots: {len(timeline)} ({sum(1 for k in timeline if k[0] == DEVICE)} devices, '
               f'{sum(1 for k in timeline if k[0] == VM)} virtual machines).')
    out.append('')
    table(out, ['gap', 'change sets'], [(label, gaps.get(label, 0)) for _, label in GAP_BUCKETS] +
          [('>30 d', gaps.get('>30 d', 0))])

    # 3. Chains per threshold
    def chains_for(limit):
        result = []
        for root, v in timeline.items():
            cur = [v[0]]
            for prev, nxt in zip(v, v[1:]):
                if nxt[0] - prev[0] <= limit:
                    cur.append(nxt)
                else:
                    result.append((root, cur))
                    cur = [nxt]
            result.append((root, cur))
        return result

    out.append('## 3. Chains per gap threshold (one root, gaps up to the threshold)')
    out.append('')
    rows = []
    for limit, label in THRESHOLDS:
        ch = chains_for(limit)
        multi = [c for c in ch if len(c[1]) > 1]
        users = [len({info[rid]['user'] for _, rid in c[1]}) for c in multi]
        mixed = sum(1 for c in multi if automation and
                    len({info[rid]['user'] in automation for _, rid in c[1]}) > 1)
        dur = [c[1][-1][0] - c[1][0][0] for c in multi]
        rows.append((label, len(ch), len(multi), max((len(c[1]) for c in ch), default=0),
                     duration_label(statistics.median(dur)) if dur else '-',
                     sum(1 for u in users if u > 1), mixed if automation else 'n/a'))
    table(out, ['threshold', 'chains', 'with 2+ sets', 'largest (sets)', 'median duration (2+)',
                'with 2+ users', 'people and automation'], rows)

    # 4. Onboarding: chains that start with the create of the root
    for limit, label in ((24 * 3600, '24 h'), (7 * 86400, '7 d')):
        ch = chains_for(limit)
        onboard = []
        for root, c in ch:
            first = info[c[0][1]]['per_root'][root]
            if any(stage_of(r, root) == 'create' for r in first):
                onboard.append((root, c))
        out.append(f'## 4. Chains that start with a device or VM create, threshold {label}: {len(onboard)}')
        out.append('')
        seqs = Counter()
        durs = defaultdict(list)
        sizes = Counter()
        for root, c in onboard:
            stages = []
            for _, rid in c:
                stages.extend(sorted({stage_of(r, root) for r in info[rid]['per_root'][root]},
                                     key=[s for s, _ in STAGES].index))
            seq = sequence(stages)
            seqs[seq] += 1
            durs[seq].append(c[-1][0] - c[0][0])
            sizes[bucket(len(c))] += 1
        table(out, ['change sets in chain', 'chains'],
              [(b, sizes.get(b, 0)) for b in ('1', '2', '3-5', '6-20', '21-100', '101-1000')])
        table(out, ['stage sequence', 'chains', 'median duration'],
              [(s, n, duration_label(statistics.median(durs[s]))) for s, n in seqs.most_common(15)])

        if limit == 7 * 86400:
            fields = Counter()
            stage_users = defaultdict(Counter)
            for root, c in onboard:
                for _, rid in c:
                    for r in info[rid]['per_root'][root]:
                        st = stage_of(r, root)
                        who = 'automation' if r['user'] in automation else ('person' if automation else 'user')
                        stage_users[st][who] += 1
                        if st == 'metadata':
                            for k in diff_keys(r):
                                fields[k] += 1
            out.append('Device or VM fields changed in these chains (field: updates):')
            out.append('')
            table(out, ['field', 'updates'], fields.most_common(20))
            out.append('Primary records per stage and actor kind in these chains:')
            out.append('')
            table(out, ['stage', 'records by actor kind'],
                  [(s, ', '.join(f'{k} {v}' for k, v in stage_users[s].most_common())) for s, _ in STAGES
                   if stage_users.get(s)])

    # 5. Sets that touch several roots: do they join chains into large groups?
    out.append('## 5. Change sets with several roots (cables between devices, bulk edits)')
    out.append('')
    multi_root = [i for i in info.values() if len(i['roots']) > 1]
    by_type = Counter()
    for i in multi_root:
        by_type[' '.join(sorted({r['changed_object_type'].split('.')[1] for r in i['primary']}))[:80]] += 1
    out.append(f'Change sets with 2+ roots: {len(multi_root)}.')
    out.append('')
    table(out, ['primary types in set', 'change sets'], by_type.most_common(10))
    for limit, label in ((24 * 3600, '24 h'),):
        parent = {}

        def find(x):
            while parent.setdefault(x, x) != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x
        nodes = {}
        for root, c in chains_for(limit):
            nodes[(root, c[0][1])] = {rid for _, rid in c}
        chain_of = defaultdict(list)
        for key, rids in nodes.items():
            for rid in rids:
                chain_of[rid].append(key)
        for rid, keys in chain_of.items():
            for k in keys[1:]:
                parent[find(keys[0])] = find(k)
        comp = Counter(find(k) for k in nodes)
        sizes = sorted(comp.values(), reverse=True)
        out.append(f'Joining chains ({label}) through shared change sets: {len(sizes)} groups; '
                   f'largest groups (chains): {sizes[:5]}.')
        out.append('')

    out.append('## 6. Other')
    out.append('')
    out.append(f'- Records without a root, by type (all records): {dict(res.unresolved.most_common(10))}.')
    out.append(f'- Run time: {time.monotonic() - started:.0f} s, API calls: {api.calls}.')
    print('\n'.join(out))


if __name__ == '__main__':
    main()
