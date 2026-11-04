"""REST API: aggregates as flat JSON arrays, for Grafana (Infinity data source)."""
from datetime import datetime

from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from netbox.api.authentication import IsAuthenticatedOrLoginNotRequired

from ..filters import InsightsFilter, local_tz
from ..insights import Insights

__all__ = (
    'ActivityView',
)

GROUPS = ('day', 'week', 'user', 'type')
MAX_LIMIT = 100


def _date_from_ms(value, name):
    try:
        return datetime.fromtimestamp(int(value) / 1000, local_tz()).date()
    except (TypeError, ValueError, OverflowError, OSError):
        raise ValidationError({name: 'Expected milliseconds since the epoch, as ${__from} and ${__to} in Grafana.'})


class ActivityView(APIView):
    """
    GET /api/plugins/changelog-insights/activity/?group=day&from=<ms>&to=<ms>

    group: day, week (time series), user or type (top list). from, to: milliseconds since the
    epoch (Grafana ${__from} and ${__to}); the range covers whole days in TIME_ZONE. Without
    them: period (7, 30, 90, custom), start, end, as on the pages. Filters: user_id,
    user_name, type_id, action, kind (person, automation; all: no filter). limit: rows of a top list (default 10, at most 100).
    Counts are primary changes; "records" counts all records, folded included. The
    setting max_period_days applies; the header X-Period-Cut-From then names the first day asked for.
    Authentication: a NetBox API token ("Authorization: Bearer nbt_<key>.<token>" or
    "Token <token>") or a session. Permission: core.view_objectchange.
    """
    permission_classes = [IsAuthenticatedOrLoginNotRequired]

    def get(self, request):
        if not request.user.has_perm('core.view_objectchange'):
            raise PermissionDenied()
        group = request.query_params.get('group', 'day')
        if group not in GROUPS:
            raise ValidationError({'group': f'One of: {", ".join(GROUPS)}.'})
        try:
            limit = min(max(int(request.query_params.get('limit', 10)), 1), MAX_LIMIT)
        except ValueError:
            raise ValidationError({'limit': 'Expected an integer.'})

        params = request.query_params.copy()
        if params.get('kind') == 'all':
            # Grafana sends the "All" value of a variable.
            params.pop('kind')
        if 'from' in params or 'to' in params:
            start = _date_from_ms(params.get('from'), 'from')
            end = _date_from_ms(params.get('to'), 'to')
            if start > end:
                raise ValidationError({'from': 'Must not be after "to".'})
            params.setlist('period', ['custom'])
            params.setlist('start', [start.isoformat()])
            params.setlist('end', [end.isoformat()])
        flt = InsightsFilter.from_params(params)
        if flt.errors:
            raise ValidationError({k: [str(e) for e in v] for k, v in flt.errors.items()})
        rows = Insights(request.user, flt).activity(group, limit)
        response = Response(rows)
        if flt.cut_from:
            response['X-Period-Cut-From'] = flt.cut_from.isoformat()
        return response
