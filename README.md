# netbox-changelog-insights

NetBox plugin: insights into the changelog, in the manner of GitHub Insights. Who changed what, when and how much, grouped by user, object type, action and time.

Status: first version. Pages: Pulse, Contributors, Chains; REST API for Grafana. Design: [docs/design.md](docs/design.md).

Target: NetBox 4.6 (`min_version` 4.6.0). No models, no migrations.

## Pages

- Pulse: change sets, records by action, primary changes, active users (people and automation), change sets per day, top users, top object types, most changed objects, recent change sets. Click a change set to show its folded records.
- Contributors: per user, change sets, created, updated, deleted, weekly change sets, top types, first and last change.
- Chains: change sets about one device or virtual machine over time (for example the onboarding of a server), with stages, duration and users. Click a chain to show its change sets.

The unit is the change set: the records that share a `request_id`. Side effects fold into their cause (design decision 3); counts and rankings use primary changes. Every number links to the core changelog (`/core/changelog/`) with the same filter. The pages require `core.view_objectchange` and show only the records that the user may view. Days and weeks use NetBox `TIME_ZONE`.

## Install

```
pip install git+https://github.com/islepnev/netbox-changelog-insights
```

`configuration.py`:

```python
PLUGINS = ['netbox_changelog_insights']
PLUGINS_CONFIG = {
    'netbox_changelog_insights': {
        'automation_group': 'automation',  # user group of automation accounts; None: no split
        'cache_ttl': 300,                  # seconds; 0 disables the cache
        'chain_gap_hours': 168,            # chains: longest gap between change sets on one device or VM
        'automation_gap_hours': 24,        # chains: longest gap that automation alone may bridge
        'max_period_days': None,           # longest period of a page, in days; None: no limit
    },
}
```

Then run `manage.py collectstatic`.

## API for Grafana

`GET /api/plugins/changelog-insights/activity/` returns aggregates as a flat JSON array, for the Grafana Infinity data source. Parameters: `group` (`day`, `week`, `user`, `type`), `from` and `to` in milliseconds (Grafana `${__from}`, `${__to}`) or `period`, `start`, `end` as on the pages, filters `user_id`, `user_name`, `type_id`, `action`, `kind` (`person`, `automation`, `all`), `limit` (top lists, default 10, at most 100). Authentication: a NetBox API token (`Authorization: Bearer nbt_<key>.<token>`); permission `core.view_objectchange`. `max_period_days` applies; the header `X-Period-Cut-From` then names the first day asked for.

```
curl -H "Authorization: Bearer nbt_<key>.<token>" \
  "https://netbox.example.com/api/plugins/changelog-insights/activity/?group=day&period=30"
[{"time": "2026-10-06T00:00:00+00:00", "change_sets": 214, "changes": 260, "records": 731, "created": 98, "updated": 141, "deleted": 21}, ...]
```

[docs/grafana-row.json](docs/grafana-row.json): a row of four panels (totals, change sets per day or week, top users, top object types) on this endpoint. Copy `panels` and `templating.list` into a dashboard; set the Infinity data source URL to the NetBox base URL and its `Authorization` header to the token.

## Development

NetBox 4.6.4 in Docker, with the plugin from this checkout:

```
docker compose -f dev/docker-compose.yml up -d
```

Open http://localhost:8001 (admin / admin). Generate a synthetic changelog (profile from `docs/design.md`; `--scale 1` is about 20,000 change sets in 90 days):

```
docker compose -f dev/docker-compose.yml exec netbox /opt/netbox/venv/bin/python /opt/netbox-changelog-insights/dev/generate_changelog.py --scale 0.25
```

Tests, in the NetBox directory with the plugin installed:

```
python manage.py test netbox_changelog_insights
```

## Fold audit

`tools/fold_audit.py` measures the fold rules on a NetBox changelog over the REST API. It sends GET requests only and prints counts, with no names or values:

```
NETBOX_URL=https://netbox.example.com NETBOX_TOKEN=... python3 tools/fold_audit.py --days 90 > fold_audit.md
```

`tools/chain_audit.py` measures chains of change sets about one device or virtual machine over time (for example the onboarding of a server). It needs `fold_audit.py` in the same directory and has the same rules: GET requests only, counts and field names only:

```
NETBOX_URL=https://netbox.example.com NETBOX_TOKEN=... python3 tools/chain_audit.py --days 90 --automation-group automation > chain_audit.md
```

`dev/generate_changelog.py --onboardings N` adds N server onboardings spread over hours and days.
