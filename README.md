# netbox-changelog-insights

NetBox plugin: insights into the changelog, in the manner of GitHub Insights. Who changed what, when and how much, grouped by user, object type, action and time.

Status: first version. Pages: Pulse, Contributors. Design: [docs/design.md](docs/design.md).

Target: NetBox 4.6 (`min_version` 4.6.0). No models, no migrations.

## Pages

- Pulse: change sets, records by action, primary changes, active users (people and automation), change sets per day, top users, top object types, most changed objects, recent change sets. Click a change set to show its folded records.
- Contributors: per user, change sets, created, updated, deleted, weekly change sets, top types, first and last change.

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
    },
}
```

Then run `manage.py collectstatic`.

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
