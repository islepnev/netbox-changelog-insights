# Design

## Goal

Make the NetBox changelog observable: who changes what, when, how much and where. The core changelog is a flat list of records; it answers "what happened to this object", not "where does the work go". The model is GitHub Insights (Pulse, Contributors, Commit activity, Code frequency); metric names follow CHAOSS where one exists.

## Source data

`core.ObjectChange`, NetBox 4.6.4 (`netbox/core/models/change_logging.py`):

| Field | Use | Index |
|---|---|---|
| `time` | time buckets | yes |
| `request_id` | change set | yes |
| `user`, `user_name` | actor; `user` is null after the user is deleted | `user` FK |
| `action` | create, update, delete | |
| `changed_object_type`, `changed_object_id` | object type, object | yes (pair) |
| `related_object_type`, `related_object_id` | parent object (device of an interface) | yes (pair) |
| `object_repr`, `message` | labels | |
| `prechange_data`, `postchange_data` | field-level diff | |

Typical shape of a changelog: most change sets hold one record; automation accounts write most records; cabling (cables, terminations, interface cable fields) leads by records, and the top updated fields are side effects of new cables.

## Mapping to GitHub Insights

| GitHub | Plugin | Source |
|---|---|---|
| Commit | Change set: records sharing `request_id` | `request_id` |
| Author | User; person or automation | `user`, `user_name` |
| Additions, deletions | Creates, updates, deletes | `action` |
| Path | App and model | `changed_object_type` |
| Pulse | Period summary | all |
| Contributors | Per-user activity over time | `user`, `time` |
| Commit activity | Weekly series; weekday × hour heatmap | `time` |
| Code frequency | Creates vs deletes per week: inventory growth | `action`, `time` |
| Hotspots (blame) | Most changed objects and fields | diff |

Dimensions for grouping and filtering: period, actor, actor kind, app, model, action, change set, field (updates only), site, tenant.

## Decisions

1. Read the changelog in place; keep no copy. Aggregates are SQL `GROUP BY` over an indexed time range, cached by filter and period with a TTL setting. Rollup tables only if a measurement shows the need. The fold queries run with PostgreSQL JIT off: their cost estimate starts JIT compilation, which takes longer than the query. Reach is bounded by `CHANGELOG_RETENTION`; every page shows the oldest available record.
2. The activity unit is the change set, not the record. Creating one cable writes the cable, two terminations and updates to both ends; counting records ranks cabling work far above everything else. Records stay as a second metric.
3. Side effects fold into their cause within a change set: a further record of the same object folds into its main record (create, else delete, else first update; NetBox writes a create and an update for each new cable); a record whose related object changed in the same change set (components created with a device); a cable termination, and an update that changes only cable fields (`cable`, `cable_end`, `cable_connector`, `cable_positions`), when a cable in the same change set changed; an update of a device or virtual machine that changes only `primary_ip4`, `primary_ip6` or `oob_ip`, when the IP address it points to (before or after) changed in the same change set ("Make this the primary IP"). Counts and rankings use primary changes; folded records open on click. Checked with `tools/fold_audit.py`: every new cable has the extra update; no cable-only update lacks a cable record; no change set loses all primary records.
4. Actor kind (person or automation) is membership in a NetBox user group named in the plugin settings. Unset: no split. No name patterns. A deleted user is shown by `user_name`.
5. Field-level changes use the same rules as `ObjectChange.diff()`: ignore `created`, `last_updated`, MPTT fields and keys starting with `_`. Computed in PostgreSQL over `jsonb` for update records.
6. Every number links to the core changelog list (`/core/changelog/`) with the same filter (user, action, type, time range, request). Field-level figures, which core cannot filter, link to the plugin's own list.
7. Permissions follow core: pages require `core.view_objectchange`; querysets use `.restrict(user, 'view')`.
8. Time buckets and the heatmap use NetBox `TIME_ZONE`.
9. UI: one "Insights" menu; NetBox templates and theme variables (light and dark); one chart library vendored as a static file, no CDN; the heatmap is an HTML table.
10. No models in v1, hence no migrations. `min_version` 4.6.0.
11. Chains: change sets about one device or virtual machine (the root) over time, for example the onboarding of a server. The root of a record is the device or VM itself, its related object, the `device` or `virtual_machine` field of its data, or the component it is attached to (cable terminations, IP and MAC addresses, FHRP assignments); a cable has the roots of its terminations. Change sets on one root join a chain when the gap is up to `chain_gap_hours` (default 168); a gap longer than `automation_gap_hours` (default 24) joins only if a person's change set follows within the chain gap, so that automation work joins a chain but periodic syncs do not stretch it. Chains are not joined through cables. Checked with `tools/chain_audit.py`: most primary records have a root; most gaps are under an hour; onboarding goes on with IP addresses days later; joining through cables forms groups of hundreds of chains.

12. Scope: the site and the tenant of a record are SQL expressions over the record, so that filters and groups stay in the database (decision 1). Site: the site itself; else the `site` field of the data; else the `_site` field that NetBox caches on components, cable terminations, prefixes and clusters; else the site of its device or virtual machine (the related object, the `device` or `virtual_machine` field, `_device` of a cable termination, or the interface an IP or MAC address is assigned to); else, for a cable, the `_site` of its terminations in the same change set, then of its current terminations. Tenant: the tenant itself; else the `tenant` field; else the tenant of the device or virtual machine. A device or virtual machine is read from its current row, else from its latest record (a deleted device). A chain is in the site and tenant of its root. The filter value `null` (as in NetBox filters) selects records without a site or tenant. Activity splits by site and tenant too. A change set that touches several sites counts in each. The core changelog cannot filter by site or tenant: its links drop that filter.

## Pages, ranked by value against effort

| Page | Content | Size |
|---|---|---|
| Pulse | Period (7, 30, 90 days, custom): change sets, records by action, active users by kind, top users, types, objects | M |
| Contributors | Per user: first and last change, change sets, creates, updates, deletes, weekly sparkline, top types | M |
| Chains | Chains per device or VM: onboarding filter, stages, duration, users; click shows the change sets | L |
| Activity | Series over time grouped by one dimension (user, app, model, action, site, tenant); weekday × hour heatmap (done) | M |
| Actor kind | Person/automation split on all pages | S |
| Hotspots | Most changed objects; most changed fields per model; values before and after per field (done) | M |
| API | Aggregates over REST for Grafana: `activity/` by day, week, user, type; Grafana row in `docs/grafana-row.json` (done) | S |
| User | One user: change sets per day, weekday × hour, sites, top types and objects, recent change sets, first and last change; the user is a filter, so every page opens for that user (done) | M |
| Scope | Per site or tenant: change sets, actions, objects, users, weekly trend, top types; site and tenant filter on all pages and the API (done) | L |

## Out of scope

- `netbox_branching`: not in use; how merged branch changes appear in the main changelog is left for later.

## Open questions

- Aggregates over long periods without rollup tables: measured with `tools/timing.py`. The fold takes most of the time (correlated subqueries per record); up to 90 days the pages stay within seconds and the cache serves repeats, long periods are slower: set `max_period_days`. The test "changes only cable (or primary IP) fields" compares those fields first and scans the other keys only when one of them changed, which halves the fold. A fold once per change set with window functions gave the same primary records but was not faster, and was dropped.
