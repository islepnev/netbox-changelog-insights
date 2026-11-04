# Development

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

## Timing

`tools/timing.py` times the pages on a NetBox database: 7, 30 and 90 days and the full history, aggregates only and full page renders, and prints `EXPLAIN ANALYZE` of the Pulse fold query with and without JIT. It sets the database session to read-only, turns the cache off in its own process, and prints timings, counts and query plans only. Run it in the NetBox directory with the NetBox Python and the plugin installed:

```
cd /opt/netbox/netbox
/opt/netbox/venv/bin/python /path/to/tools/timing.py > timing.txt
```

## Native NetBox for tests

Faster than Docker: clone NetBox at the tag to test (`git clone --depth 1 --branch v4.6.4 https://github.com/netbox-community/netbox.git`), install its `requirements.txt` in a venv (`psycopg[binary,pool]` in place of `psycopg[c,pool]`) and `pip install -e` this checkout. A `configuration.py` as in `.github/workflows/test.yml` works with a local PostgreSQL and Redis. Then, in `netbox/netbox`:

```
python manage.py test netbox_changelog_insights --keepdb --parallel 1
```
