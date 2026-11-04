#!/usr/bin/env python3
"""Measure candidate fold rules on a NetBox changelog over the REST API.

Read only: the script sends GET requests and nothing else. It prints a
Markdown report of counts by object type, action and rule. The report
contains no object names, user names, messages or field values.

Usage:
    NETBOX_URL=https://netbox.example.com NETBOX_TOKEN=... \
        python3 fold_audit.py --days 90 > fold_audit.md

Python 3.10 or later, standard library only.
"""
import argparse
import json
import os
import random
import ssl
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

# Fields that NetBox sets on a cabled object when a cable connects or
# disconnects it (CabledObjectModel.set_cable_termination(), NetBox 4.6).
CABLE_FIELDS = {'cable', 'cable_end', 'cable_connector', 'cable_positions'}

# Fields of a device or virtual machine that point to its primary IP addresses.
PRIMARY_IP_FIELDS = {'primary_ip4', 'primary_ip6', 'oob_ip'}

# Fields that ObjectChange.diff() ignores, plus keys that start with "_".
DIFF_EXCLUDE = {'created', 'last_updated', 'level', 'lft', 'rght', 'tree_id'}

# Related object that each model writes in to_objectchange() (NetBox 4.6.4).
# Value: field with the parent id, or (type field, id field) for a generic FK.
DCIM_COMPONENTS = (
    'consoleport', 'consoleserverport', 'powerport', 'poweroutlet', 'interface',
    'frontport', 'rearport', 'modulebay', 'devicebay', 'inventoryitem',
)
RELATED = {f'dcim.{m}': ('dcim.device', 'device') for m in DCIM_COMPONENTS}
RELATED.update({
    'dcim.cabletermination': ('termination_type', 'termination_id'),
    'dcim.rackreservation': ('dcim.rack', 'rack'),
    'virtualization.vminterface': ('virtualization.virtualmachine', 'virtual_machine'),
    'virtualization.virtualdisk': ('virtualization.virtualmachine', 'virtual_machine'),
    'ipam.ipaddress': ('assigned_object_type', 'assigned_object_id'),
    'ipam.vlantranslationrule': ('ipam.vlantranslationpolicy', 'policy'),
    'circuits.circuittermination': ('circuits.circuit', 'circuit'),
    'circuits.virtualcircuittermination': ('circuits.virtualcircuit', 'virtual_circuit'),
    'tenancy.contactassignment': ('object_type', 'object_id'),
    'vpn.tunneltermination': ('vpn.tunnel', 'tunnel'),
    'extras.imageattachment': ('object_type', 'object_id'),
})
TEMPLATE_MODELS = (
    'consoleporttemplate', 'consoleserverporttemplate', 'powerporttemplate', 'poweroutlettemplate',
    'interfacetemplate', 'frontporttemplate', 'rearporttemplate', 'modulebaytemplate',
    'devicebaytemplate', 'inventoryitemtemplate',
)

FIELDS = 'id,time,request_id,user,action,changed_object_type,changed_object_id,prechange_data,postchange_data'


class Api:
    def __init__(self, url, token, insecure=False):
        self.url = url.rstrip('/')
        self.token = token
        self.ctx = ssl._create_unverified_context() if insecure else None
        self.calls = 0

    def get(self, path, params=None):
        query = urllib.parse.urlencode(params or {}, doseq=True)
        req = urllib.request.Request(
            f'{self.url}/api/{path}?{query}',
            headers={'Authorization': f'Token {self.token}', 'Accept': 'application/json'},
            method='GET',
        )
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, context=self.ctx, timeout=300) as resp:
                    self.calls += 1
                    return json.load(resp)
            except OSError:
                if attempt == 3:
                    raise
                time.sleep(2 ** attempt)

    def count(self, path, params):
        return self.get(path, {**params, 'limit': 1, 'fields': 'id'})['count']


def data_of(rec):
    return rec['postchange_data'] or rec['prechange_data'] or {}


def diff_keys(rec):
    pre = rec['prechange_data'] or {}
    post = rec['postchange_data'] or {}
    keys = set(pre) | set(post)
    return frozenset(
        k for k in keys
        if not k.startswith('_') and k not in DIFF_EXCLUDE and pre.get(k) != post.get(k)
    )


def related_of(rec, ct_by_id):
    """Return (app.model, id) of the related object, inferred from record data."""
    t = rec['changed_object_type']
    data = data_of(rec)
    rule = RELATED.get(t)
    if rule is None and t.startswith('dcim.') and t.split('.')[1] in TEMPLATE_MODELS:
        if data.get('device_type'):
            return ('dcim.devicetype', data['device_type'])
        if data.get('module_type'):
            return ('dcim.moduletype', data['module_type'])
        return None
    if rule is None:
        return None
    type_field, id_field = rule
    if '.' in type_field:
        rel_type = type_field
    else:
        rel_type = ct_by_id.get(data.get(type_field))
    rel_id = data.get(id_field)
    if rel_type is None or rel_id is None:
        return None
    return (rel_type, rel_id)


def bucket(n):
    for limit, label in ((1, '1'), (2, '2'), (5, '3-5'), (20, '6-20'), (100, '21-100'), (1000, '101-1000')):
        if n <= limit:
            return label
    return '>1000'


def signature(recs, limit=8):
    c = Counter(f"{r['changed_object_type'].split('.')[1]}:{r['action']}" for r in recs)
    parts = [f'{k}x{v}' for k, v in sorted(c.items(), key=lambda kv: (-kv[1], kv[0]))]
    if len(parts) > limit:
        parts = parts[:limit] + ['...']
    return ' '.join(parts)


def table(out, header, rows):
    out.append('| ' + ' | '.join(header) + ' |')
    out.append('|' + '---|' * len(header))
    for row in rows:
        out.append('| ' + ' | '.join(str(x) for x in row) + ' |')
    out.append('')


def fold(changeset, ct_by_id, rules):
    """Return {record id: (rule, parent record id)} for folded records.

    Rules, in order of precedence:
      repeat   a record for an object whose main record (create, delete, first update) is another one
      cable    a cable termination, when a cable record is in the set
      cablef   an update that changes only cable fields, when a cable record is in the set
      primaryip a device or VM update of primary IP fields only, when that IP address has a record in the set
      related  the related object has a record in the set
      module   a component whose module has a create or delete record in the set
    """
    # Main record of an object: its create, else its delete, else its first update.
    rank = {'create': 0, 'delete': 1}
    by_obj = {}
    for r in sorted(changeset, key=lambda r: (rank.get(r['action'], 2), r['id'])):
        by_obj.setdefault((r['changed_object_type'], r['changed_object_id']), r)
    cables = [r for r in changeset if r['changed_object_type'] == 'dcim.cable']
    cable_by_id = {k[1]: v for k, v in by_obj.items() if k[0] == 'dcim.cable'}
    folded = {}
    for r in changeset:
        key = (r['changed_object_type'], r['changed_object_id'])
        first = by_obj[key]
        data = data_of(r)
        if 'repeat' in rules and first['id'] != r['id']:
            folded[r['id']] = ('repeat', first['id'])
            continue
        if cables and 'cable' in rules and r['changed_object_type'] == 'dcim.cabletermination':
            parent = cable_by_id.get(data.get('cable')) or cables[0]
            folded[r['id']] = ('cable', parent['id'])
            continue
        if cables and 'cablef' in rules and r['action'] == 'update':
            keys = diff_keys(r)
            if keys and keys <= CABLE_FIELDS:
                pre = (r['prechange_data'] or {}).get('cable')
                post = (r['postchange_data'] or {}).get('cable')
                parent = cable_by_id.get(post) or cable_by_id.get(pre) or cables[0]
                folded[r['id']] = ('cablef', parent['id'])
                continue
        if 'primaryip' in rules and r['action'] == 'update' \
                and r['changed_object_type'] in ('dcim.device', 'virtualization.virtualmachine'):
            keys = diff_keys(r)
            if keys and keys <= PRIMARY_IP_FIELDS:
                refs = {(r['prechange_data'] or {}).get(k) for k in keys} | {(r['postchange_data'] or {}).get(k) for k in keys}
                ips = [by_obj[('ipam.ipaddress', i)] for i in refs if ('ipam.ipaddress', i) in by_obj]
                if ips:
                    folded[r['id']] = ('primaryip', ips[0]['id'])
                    continue
        if 'related' in rules:
            rel = related_of(r, ct_by_id)
            if rel and rel in by_obj and by_obj[rel]['id'] != r['id']:
                folded[r['id']] = ('related', by_obj[rel]['id'])
                continue
        if 'module' in rules and data.get('module') and r['changed_object_type'].startswith('dcim.'):
            m = by_obj.get(('dcim.module', data['module']))
            if m and m['action'] in ('create', 'delete'):
                folded[r['id']] = ('module', m['id'])
                continue
    return folded


def fetch_object_types(api):
    """{content type id: "app_label.model"}"""
    ct_by_id = {}
    params = {'limit': 1000, 'offset': 0}
    while True:
        page = api.get('core/object-types/', params)
        for t in page['results']:
            ct_by_id[t['id']] = f"{t['app_label']}.{t['model']}"
        if not page['next']:
            break
        params['offset'] += 1000
    return ct_by_id


def fetch_records(api, since, page_size=1000):
    """All change records since the given time, in id order, with the fields the audits use."""
    records = []
    last_id = 0
    while True:
        page = api.get('core/object-changes/', {
            'time_after': since, 'id__gt': last_id, 'ordering': 'id', 'limit': page_size, 'fields': FIELDS,
        })
        for r in page['results']:
            records.append({
                'id': r['id'],
                'time': r['time'],
                'request_id': r['request_id'],
                'user': (r['user'] or {}).get('id') or f"deleted:{hash(r.get('user_name', ''))}",
                'action': r['action']['value'],
                'changed_object_type': r['changed_object_type'],
                'changed_object_id': r['changed_object_id'],
                'prechange_data': r['prechange_data'],
                'postchange_data': r['postchange_data'],
            })
        if not page['results'] or not page['next']:
            break
        last_id = page['results'][-1]['id']
        print(f'fetched {len(records)}', file=sys.stderr)
    return records


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--days', type=int, default=90)
    ap.add_argument('--page', type=int, default=1000)
    ap.add_argument('--samples', type=int, default=10, help='exact related-object checks per object type')
    ap.add_argument('--insecure', action='store_true', help='do not verify the TLS certificate')
    args = ap.parse_args()

    api = Api(os.environ['NETBOX_URL'], os.environ['NETBOX_TOKEN'], args.insecure)
    started = time.monotonic()
    since = (datetime.now(timezone.utc) - timedelta(days=args.days)).strftime('%Y-%m-%dT%H:%M:%SZ')

    ct_by_id = fetch_object_types(api)
    records = fetch_records(api, since, args.page)
    fetch_seconds = time.monotonic() - started

    sets = defaultdict(list)
    for r in records:
        sets[r['request_id']].append(r)

    out = []
    out.append(f'# Fold audit, last {args.days} days (since {since})')
    out.append('')
    out.append(f'Records fetched: {len(records)}, change sets: {len(sets)}, '
               f'fetch time: {fetch_seconds:.0f} s, API calls: {api.calls}.')
    out.append('')

    # 1. Totals
    out.append('## 1. Totals')
    out.append('')
    table(out, ['action', 'records'], sorted(Counter(r['action'] for r in records).items()))
    sizes = Counter(bucket(len(v)) for v in sets.values())
    order = ['1', '2', '3-5', '6-20', '21-100', '101-1000', '>1000']
    table(out, ['records in set', 'change sets'], [(b, sizes.get(b, 0)) for b in order])

    # 2. Inference check against the API filters on related_object_type/related_object_id.
    out.append('## 2. Related object: inferred from data vs. API filter')
    out.append('')
    out.append('The REST API does not return related_object_*; the script infers it from record data '
               'and checks the inference with the API filters.')
    out.append('')
    inferred = Counter()
    rel_of = {}
    for r in records:
        rel = related_of(r, ct_by_id)
        rel_of[r['id']] = rel
        if rel:
            inferred[(r['changed_object_type'], rel[0])] += 1
    total_rel_api = api.count('core/object-changes/', {'time_after': since, 'related_object_id__gte': 0})
    out.append(f'Records with a related object: inferred {sum(inferred.values())}, API {total_rel_api}.')
    out.append('')
    rows = []
    for (t, rt), n in inferred.most_common():
        n_api = api.count('core/object-changes/', {
            'time_after': since, 'changed_object_type': t, 'related_object_type': rt,
        })
        sample = [r for r in records if r['changed_object_type'] == t and rel_of[r['id']] and rel_of[r['id']][0] == rt]
        sample = random.sample(sample, min(args.samples, len(sample)))
        exact = sum(
            api.count('core/object-changes/', {
                'id': r['id'], 'related_object_type': rt, 'related_object_id': rel_of[r['id']][1],
            }) for r in sample
        )
        rows.append((t, rt, n, n_api, f'{exact}/{len(sample)}'))
    table(out, ['changed type', 'related type', 'inferred', 'API', 'exact sample'], rows)

    # 3. Rules, in order of precedence.
    rule_sets = {
        'design (repeat off)': ('cable', 'cablef', 'related'),
        'design + repeat': ('repeat', 'cable', 'cablef', 'related'),
        'design + repeat + module': ('repeat', 'cable', 'cablef', 'related', 'module'),
        'design + repeat + primary ip': ('repeat', 'cable', 'cablef', 'primaryip', 'related'),
    }
    results = {}
    for name, rules in rule_sets.items():
        folded = {}
        for cs in sets.values():
            folded.update(fold(cs, ct_by_id, rules))
        results[name] = folded
    out.append('## 3. Folded records per rule set')
    out.append('')
    rows = []
    for name, folded in results.items():
        by_rule = Counter(v[0] for v in folded.values())
        primary_sets = Counter()
        for rid, cs in sets.items():
            primary_sets[sum(1 for r in cs if r['id'] not in folded)] += 1
        rows.append((
            name, len(folded), f'{100 * len(folded) / max(len(records), 1):.1f}%',
            ', '.join(f'{k} {v}' for k, v in sorted(by_rule.items())),
            primary_sets.get(0, 0),
        ))
    table(out, ['rule set', 'folded', 'share', 'by rule', 'sets with 0 primaries'], rows)

    main_set = results['design + repeat + primary ip']
    rec_by_id = {r['id']: r for r in records}
    out.append('## 4. Folded records by rule, type and action ("design + repeat + primary ip")')
    out.append('')
    c = Counter((v[0], rec_by_id[k]['changed_object_type'], rec_by_id[k]['action'],
                 rec_by_id[v[1]]['changed_object_type'], rec_by_id[v[1]]['action'])
                for k, v in main_set.items())
    table(out, ['rule', 'type', 'action', 'parent type', 'parent action', 'records'],
          [(*k, n) for k, n in c.most_common(40)])

    # 5. Cable fields
    out.append('## 5. Updates that touch cable fields')
    out.append('')
    c = Counter()
    for cs in sets.values():
        has_cable = any(r['changed_object_type'] == 'dcim.cable' for r in cs)
        for r in cs:
            if r['action'] != 'update':
                continue
            keys = diff_keys(r)
            if keys & CABLE_FIELDS:
                c[(r['changed_object_type'], ','.join(sorted(keys)) if len(keys) <= 6 else f'{len(keys)} fields',
                   'yes' if has_cable else 'no')] += 1
    table(out, ['type', 'changed fields', 'cable record in set', 'records'], [(*k, n) for k, n in c.most_common(25)])

    # 6. Shapes of typical change sets
    out.append('## 6. Change set shapes (top 10 per trigger)')
    out.append('')
    triggers = [
        ('dcim.cable', 'create'), ('dcim.cable', 'update'), ('dcim.cable', 'delete'),
        ('dcim.device', 'create'), ('dcim.device', 'delete'), ('dcim.module', 'create'),
        ('dcim.module', 'delete'), ('virtualization.virtualmachine', 'create'),
    ]
    for t, a in triggers:
        cs_list = [cs for cs in sets.values() if any(r['changed_object_type'] == t and r['action'] == a for r in cs)]
        if not cs_list:
            continue
        out.append(f'### Sets with {t} {a}: {len(cs_list)}')
        out.append('')
        c = Counter(signature(cs) for cs in cs_list)
        table(out, ['records (type:action x count)', 'sets'], c.most_common(10))

    # 7. What stays primary in sets with several primaries
    out.append('## 7. Sets with more than one primary record ("design + repeat + primary ip")')
    out.append('')
    hist = Counter()
    sig = Counter()
    for cs in sets.values():
        prim = [r for r in cs if r['id'] not in main_set]
        hist[bucket(len(prim))] += 1
        if len(prim) > 1:
            sig[signature(prim)] += 1
    table(out, ['primary records in set', 'change sets'], [(b, hist.get(b, 0)) for b in order])
    table(out, ['primary records (type:action x count)', 'sets'], sig.most_common(20))

    # 8. Ranking of types: records vs. primary records vs. change sets
    out.append('## 8. Top types: records, primary records, change sets')
    out.append('')
    rec_c = Counter(r['changed_object_type'] for r in records)
    prim_c = Counter(r['changed_object_type'] for r in records if r['id'] not in main_set)
    set_c = Counter()
    for cs in sets.values():
        for t in {r['changed_object_type'] for r in cs if r['id'] not in main_set}:
            set_c[t] += 1
    table(out, ['type', 'records', 'primary', 'change sets'],
          [(t, n, prim_c.get(t, 0), set_c.get(t, 0)) for t, n in rec_c.most_common(15)])

    # 9. Top users, anonymized
    out.append('## 9. Top 5 users (anonymized): records, primary, change sets')
    out.append('')
    users = Counter(r['user'] for r in records)
    rows = []
    for i, (u, n) in enumerate(users.most_common(5), 1):
        prim = sum(1 for r in records if r['user'] == u and r['id'] not in main_set)
        nsets = len({r['request_id'] for r in records if r['user'] == u})
        rows.append((f'user {i}', n, prim, nsets))
    table(out, ['user', 'records', 'primary', 'change sets'], rows)

    # 11. Fields of primary updates in sets with several primary records
    out.append('## 11. Changed fields of primary updates in sets with more than one primary record')
    out.append('')
    c = Counter()
    for cs in sets.values():
        prim = [r for r in cs if r['id'] not in main_set]
        if len(prim) < 2:
            continue
        others = ' '.join(sorted({f"{r['changed_object_type'].split('.')[1]}:{r['action']}" for r in prim}))
        for r in prim:
            if r['action'] != 'update':
                continue
            keys = diff_keys(r)
            c[(r['changed_object_type'], ','.join(sorted(keys)), others)] += 1
    table(out, ['type', 'changed fields', 'primary records in set (type:action)', 'records'],
          [(*k, n) for k, n in c.most_common(25)])

    # 10. Empty updates and records with a message
    empty = sum(1 for r in records if r['action'] == 'update' and not diff_keys(r))
    out.append('## 10. Other')
    out.append('')
    out.append(f'- Updates with no field change: {empty}.')
    out.append(f'- Change sets with records of more than one user: '
               f'{sum(1 for cs in sets.values() if len({r["user"] for r in cs}) > 1)}.')
    span = Counter()
    for cs in sets.values():
        ts = sorted(r['time'] for r in cs)
        d = (datetime.fromisoformat(ts[-1]) - datetime.fromisoformat(ts[0])).total_seconds()
        span['<1 s' if d < 1 else '1-60 s' if d < 60 else '1-60 min' if d < 3600 else '>1 h'] += 1
    out.append(f'- Time from first to last record in a set: {dict(span)}.')
    out.append(f'- Total run time: {time.monotonic() - started:.0f} s, API calls: {api.calls}.')
    print('\n'.join(out))


if __name__ == '__main__':
    main()
