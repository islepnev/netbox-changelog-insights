import json
from urllib.parse import urlencode

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.mixins import AccessMixin
from django.http import Http404
from django.shortcuts import render
from django.views.generic import View

from netbox.plugins import get_plugin_config

from . import charts
from .filters import PERIODS, InsightsFilter, changeset_changelog_url
from .folding import fold_tree
from .insights import Insights

__all__ = (
    'ActivityView',
    'FieldChangesView',
    'HotspotsView',
    'ChainsView',
    'ChangeSetView',
    'ContributorsView',
    'PulseView',
    'ScopeView',
    'UserView',
)


class InsightsView(AccessMixin, View):
    """
    Base view: the same permission as the core changelog (design decision 7). NetBox's
    permission backend answers has_perm(), so LOGIN_REQUIRED, EXEMPT_VIEW_PERMISSIONS
    and object permissions apply as in core views.
    """
    template_name = None
    viewname = None
    permission = 'core.view_objectchange'

    def dispatch(self, request, *args, **kwargs):
        user = request.user
        if getattr(settings, 'LOGIN_REQUIRED', True) and not user.is_authenticated:
            return self.handle_no_permission()
        if not user.has_perm(self.permission):
            return self.handle_no_permission()
        return super().dispatch(request, *args, **kwargs)

    def user_filter_url(self, flt, u, viewname=None):
        """Link to this page (or viewname) filtered by one user."""
        if u['user_id']:
            return flt.url(viewname or self.viewname, user_id=u['user_id'], user_name=None, kind=None)
        return flt.url(viewname or self.viewname, user_id=None, user_name=u['user_name'], kind=None)

    def base_context(self, request, flt, insights):
        user_label = None
        if flt.user_id:
            user_label = get_user_model().objects.filter(pk=flt.user_id).values_list('username', flat=True).first()
        return {
            'TIME_ZONE_NAME': settings.TIME_ZONE,
            'tab_query': urlencode(flt.params()),
            'filter_user_label': user_label,
            'flt': flt,
            'periods': PERIODS,
            'viewname': self.viewname,
            'oldest': insights.oldest_record(),
            'automation_group': insights.automation_group,
            'period_urls': [(str(p), flt.url(self.viewname, period=p, start=None, end=None)) for p in PERIODS],
            'clear_url': flt.url(
                self.viewname, user_id=None, user_name=None, type_id=None, action=None, kind=None, onboarding=None,
                site_id=None, tenant_id=None,
            ),
        }


class PulseView(InsightsView):
    template_name = 'netbox_changelog_insights/pulse.html'
    viewname = 'plugins:netbox_changelog_insights:pulse'

    def get(self, request):
        flt = InsightsFilter.from_request(request)
        insights = Insights(request.user, flt)
        pulse = insights.pulse()
        context = self.base_context(request, flt, insights)
        for r in pulse['top_types']:
            r['filter_url'] = flt.url(self.viewname, type_id=r['changed_object_type_id'])
        for u in pulse['top_users']:
            u['filter_url'] = self.user_filter_url(flt, u, 'plugins:netbox_changelog_insights:user')
        contributors = 'plugins:netbox_changelog_insights:contributors'
        context.update({
            'contributors_url': flt.url(contributors),
            'action_urls': {a: flt.changelog_url(action=a) for a in ('create', 'update', 'delete')},
            'person_url': flt.url(contributors, kind='person'),
            'automation_url': flt.url(contributors, kind='automation'),
            'pulse': pulse,
            'daily_chart': charts.bar_chart(pulse['daily']),
            'recent': pulse['recent'],
            'automation_ids': insights.automation_user_ids(),
        })
        return render(request, self.template_name, context)


class UserView(InsightsView):
    """One user: Pulse of the user, weekday and hour, sites. The user comes from the filter."""
    template_name = 'netbox_changelog_insights/user_detail.html'
    viewname = 'plugins:netbox_changelog_insights:user'

    def get(self, request):
        flt = InsightsFilter.from_request(request)
        if not flt.user_id and not flt.user_name:
            raise Http404
        insights = Insights(request.user, flt)
        pulse = insights.pulse()
        activity = insights.activity_page('model', 'day')
        sites = insights.scope_page('site')
        span = insights.user_span()
        if span['first'] is None:
            raise Http404
        context = self.base_context(request, flt, insights)
        name = context['filter_user_label'] or flt.user_name
        for r in pulse['top_types']:
            r['filter_url'] = flt.url('plugins:netbox_changelog_insights:pulse', type_id=r['changed_object_type_id'])
        scoped = sum(g['records'] for g in sites['groups'] if g['key'] is not None)
        site_rows = [g for g in sites['groups'] if g['key'] is not None][:10]
        for g in site_rows:
            g['filter_url'] = flt.url('plugins:netbox_changelog_insights:pulse', site_id=g['key'])
        user_params = {'user_id': flt.user_id} if flt.user_id else {'user_name': flt.user_name}
        context['period_urls'] = [
            (str(p), flt.url(self.viewname, period=p, start=None, end=None)) for p in PERIODS
        ]
        # Clear keeps the user: the user is the subject of this page, not a filter.
        context['clear_url'] = flt.url(
            self.viewname, type_id=None, action=None, kind=None, onboarding=None, site_id=None, tenant_id=None,
        )
        context.update({
            'name': name,
            'deleted': not flt.user_id,
            'span': span,
            'pulse': pulse,
            'daily_chart': charts.bar_chart(pulse['daily']),
            'heat': charts.heatmap(activity['heat']),
            'recent': pulse['recent'],
            'sites': site_rows,
            'no_site': pulse['primary']['records'] - scoped,
            'action_urls': {a: flt.changelog_url(action=a) for a in ('create', 'update', 'delete')},
            'activity_url': flt.url('plugins:netbox_changelog_insights:activity', by='model'),
            'chains_url': flt.url('plugins:netbox_changelog_insights:chains'),
            'contributors_url': flt.url('plugins:netbox_changelog_insights:contributors', user_id=None,
                                        user_name=None),
            'user_params': user_params,
        })
        return render(request, self.template_name, context)


class ContributorsView(InsightsView):
    template_name = 'netbox_changelog_insights/contributors.html'
    viewname = 'plugins:netbox_changelog_insights:contributors'

    def get(self, request):
        flt = InsightsFilter.from_request(request)
        insights = Insights(request.user, flt)
        data = insights.contributors()
        peak = max((max(u['trend'], default=0) for u in data['users']), default=0)
        for u in data['users']:
            u['filter_url'] = self.user_filter_url(flt, u, 'plugins:netbox_changelog_insights:user')
            u['sparkline'] = charts.sparkline(u['trend'], peak, data['buckets'], flt.start_date, flt.end_date, data['step'])
        context = self.base_context(request, flt, insights)
        context.update(data)
        context['kind_urls'] = {
            'all': flt.url(self.viewname, kind=None),
            'person': flt.url(self.viewname, kind='person'),
            'automation': flt.url(self.viewname, kind='automation'),
        }
        return render(request, self.template_name, context)


class ActivityView(InsightsView):
    template_name = 'netbox_changelog_insights/activity.html'
    viewname = 'plugins:netbox_changelog_insights:activity'
    dimensions = (
        ('action', 'Action'), ('user', 'User'), ('app', 'App'), ('model', 'Object type'), ('site', 'Site'),
        ('tenant', 'Tenant'),
    )

    def get(self, request):
        flt = InsightsFilter.from_request(request)
        by = request.GET.get('by')
        by = by if by in dict(self.dimensions) else 'action'
        bucket = request.GET.get('bucket')
        bucket = bucket if bucket in ('day', 'week') else ('day' if flt.days <= 92 else 'week')
        insights = Insights(request.user, flt)
        data = insights.activity_page(by, bucket)
        chart = charts.stacked_chart(data, by)
        # A group links to this page filtered by it and split by the next dimension (drill down).
        drill = {
            'action': 'user', 'user': 'model', 'app': 'model', 'model': 'user', 'site': 'user', 'tenant': 'user',
        }[by]
        total = sum(g['total'] for g in chart['groups'])
        for g in chart['groups']:
            share = 100 * g['total'] / total if total else 0
            g['share'] = '<1%' if 0 < share < 1 else f'{share:.0f}%'
            g['filter_url'] = flt.url(self.viewname, by=drill, bucket=bucket, **g['params']) if g['params'] else None
        context = self.base_context(request, flt, insights)
        extra = {'by': by, 'bucket': bucket}
        context['tab_query'] = urlencode(flt.params())
        context['period_urls'] = [
            (str(p), flt.url(self.viewname, period=p, start=None, end=None, **extra)) for p in PERIODS
        ]
        context['clear_url'] = flt.url(
            self.viewname, user_id=None, user_name=None, type_id=None, action=None, kind=None, onboarding=None,
            site_id=None, tenant_id=None, **extra,
        )
        context.update({
            'by': by,
            'bucket': bucket,
            'chart': chart,
            'heat': charts.heatmap(data['heat']),
            'extra_params': extra,
            'by_urls': [(k, label, flt.url(self.viewname, by=k, bucket=bucket)) for k, label in self.dimensions],
            'bucket_urls': [(b, flt.url(self.viewname, by=by, bucket=b)) for b in ('day', 'week')],
        })
        return render(request, self.template_name, context)


class ScopeView(InsightsView):
    """Activity per site or per tenant (design decision 12)."""
    template_name = 'netbox_changelog_insights/scope.html'
    viewname = 'plugins:netbox_changelog_insights:scope'
    dimensions = (('site', 'Site'), ('tenant', 'Tenant'))

    def get(self, request):
        flt = InsightsFilter.from_request(request)
        by = request.GET.get('by')
        by = by if by in dict(self.dimensions) else 'site'
        insights = Insights(request.user, flt)
        data = insights.scope_page(by)
        peak = max((max(g['trend'], default=0) for g in data['groups']), default=0)
        pulse = 'plugins:netbox_changelog_insights:pulse'
        for g in data['groups']:
            g['sparkline'] = charts.sparkline(g['trend'], peak, data['buckets'], flt.start_date, flt.end_date, data['step'])
            # Records without a site or tenant: "null", as in NetBox filters.
            value = {f'{by}_id': 'null' if g['key'] is None else g['key']}
            g['filter_url'] = flt.url(pulse, **value)
            g['activity_url'] = flt.url('plugins:netbox_changelog_insights:activity', by='user', **value)
            for t in g['top_types']:
                t['filter_url'] = flt.url(pulse, type_id=t['type_id'], **value)
        total = data['total']
        scoped = sum(g['records'] for g in data['groups'] if g['key'] is not None)
        context = self.base_context(request, flt, insights)
        extra = {'by': by}
        context['period_urls'] = [
            (str(p), flt.url(self.viewname, period=p, start=None, end=None, **extra)) for p in PERIODS
        ]
        context['clear_url'] = flt.url(
            self.viewname, user_id=None, user_name=None, type_id=None, action=None, kind=None, onboarding=None,
            site_id=None, tenant_id=None, **extra,
        )
        context.update({
            'by': by,
            'by_label': dict(self.dimensions)[by],
            'extra_params': extra,
            'groups': data['groups'],
            'named': sum(1 for g in data['groups'] if g['key'] is not None),
            'total': total,
            'share': f'{100 * scoped / total["records"]:.0f}%' if total['records'] else None,
            'by_urls': [(k, label, flt.url(self.viewname, by=k)) for k, label in self.dimensions],
        })
        return render(request, self.template_name, context)


class HotspotsView(InsightsView):
    template_name = 'netbox_changelog_insights/hotspots.html'
    viewname = 'plugins:netbox_changelog_insights:hotspots'

    def get(self, request):
        flt = InsightsFilter.from_request(request)
        insights = Insights(request.user, flt)
        data = insights.hotspots()
        for o in data['objects']:
            o['filter_url'] = flt.url(self.viewname, type_id=o['type_id'])
        for f in data['fields']:
            f['filter_url'] = flt.url(self.viewname, type_id=f['type_id'])
            f['changes_url'] = flt.url('plugins:netbox_changelog_insights:field_changes', type_id=f['type_id'],
                                       field=f['field'])
        context = self.base_context(request, flt, insights)
        context.update(data)
        return render(request, self.template_name, context)


class FieldChangesView(InsightsView):
    """Primary updates of one object type that changed one field: core cannot filter by field (decision 6)."""
    template_name = 'netbox_changelog_insights/field_changes.html'
    viewname = 'plugins:netbox_changelog_insights:field_changes'

    def get(self, request):
        flt = InsightsFilter.from_request(request)
        field = request.GET.get('field', '')[:100]
        if not flt.type_id or not field:
            raise Http404
        insights = Insights(request.user, flt)
        data = insights.field_changes(flt.type_id, field)
        for r in data['rows']:
            r['before'] = _short_json(r['before'])
            r['after'] = _short_json(r['after'])
        context = self.base_context(request, flt, insights)
        context.update(data)
        context.update({
            'field': field,
            'extra_params': {'field': field},
            'hotspots_url': flt.url('plugins:netbox_changelog_insights:hotspots'),
            'period_urls': [(str(p), flt.url(self.viewname, period=p, start=None, end=None, field=field))
                            for p in PERIODS],
            'clear_url': flt.url(self.viewname, user_id=None, user_name=None, action=None, kind=None, site_id=None,
                                 tenant_id=None, field=field),
        })
        return render(request, self.template_name, context)


def _short_json(value, limit=120):
    """A field value as compact JSON text, cut to limit characters; None stays None (field absent)."""
    if value is None:
        return None
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, separators=(',', ': '))
    return text if len(text) <= limit else text[:limit - 1] + '…'


class ChainsView(InsightsView):
    template_name = 'netbox_changelog_insights/chains.html'
    viewname = 'plugins:netbox_changelog_insights:chains'

    def get(self, request):
        flt = InsightsFilter.from_request(request)
        insights = Insights(request.user, flt)
        data = insights.chains()
        context = self.base_context(request, flt, insights)
        context.update(data)
        context['chain_gap'] = f"{get_plugin_config('netbox_changelog_insights', 'chain_gap_hours', 7 * 24)} h"
        context['automation_gap'] = f"{get_plugin_config('netbox_changelog_insights', 'automation_gap_hours', 24)} h"
        context['onboarding_urls'] = {
            'all': flt.url(self.viewname, onboarding=None),
            'onboarding': flt.url(self.viewname, onboarding=1),
        }
        context['kind_urls'] = {
            'all': flt.url(self.viewname, kind=None),
            'person': flt.url(self.viewname, kind='person'),
            'automation': flt.url(self.viewname, kind='automation'),
        }
        return render(request, self.template_name, context)


class ChangeSetView(InsightsView):
    """Records of one change set, folded records under their cause. HTMX fragment or page."""
    template_name = 'netbox_changelog_insights/changeset.html'
    viewname = 'plugins:netbox_changelog_insights:pulse'

    def get(self, request, request_id):
        flt = InsightsFilter.from_request(request)
        insights = Insights(request.user, flt)
        records = insights.change_set(request_id)
        if not records:
            raise Http404
        tree = fold_tree(records)
        context = {
            'request_id': request_id,
            'tree': tree,
            'records': records,
            'first': records[0],
            'folded': sum(len(f) for _, f in tree),
            'changelog_url': changeset_changelog_url(request_id),
            'flt': flt,
        }
        if request.htmx:
            return render(request, 'netbox_changelog_insights/inc/changeset_tree.html', context)
        return render(request, self.template_name, context)
