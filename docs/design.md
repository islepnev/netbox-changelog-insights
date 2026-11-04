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

Dimensions for grouping and filtering: period, actor, actor kind, app, model, action, change set, field (updates only). Scope (site, tenant) is a later phase.

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

## Pages, ranked by value against effort

| Page | Content | Size |
|---|---|---|
| Pulse | Period (7, 30, 90 days, custom): change sets, records by action, active users by kind, top users, types, objects | M |
| Contributors | Per user: first and last change, change sets, creates, updates, deletes, weekly sparkline, top types | M |
| Activity | Series over time grouped by one dimension (user, app, model, action); weekday × hour heatmap | M |
| Actor kind | Person/automation split on all pages | S |
| Hotspots | Most changed objects; most changed fields per model | M |
| API | Aggregates over REST for Grafana | S |
| Scope | Grouping by site and tenant, resolved from object data or the related object | L |

## Out of scope

- `netbox_branching`: not in use; how merged branch changes appear in the main changelog is left for later.

## Open questions

- Whether aggregates over long periods stay fast without rollup tables: measure with `tools/timing.py`; `max_period_days` bounds long periods.