from urllib.parse import urlencode

from django.conf import settings
from django.contrib.auth import get_user_model
from django.http import Http404
from django.shortcuts import render
from django.views.generic import View

from utilities.views import ContentTypePermissionRequiredMixin

from . import charts
from .filters import PERIODS, InsightsFilter, changeset_changelog_url
from .folding import fold_tree
from .insights import Insights

__all__ = (
    'ChangeSetView',
    'ContributorsView',
    'PulseView',
)


class InsightsView(ContentTypePermissionRequiredMixin, View):
    """Base view: the same permission as the core changelog (design decision 7)."""
    template_name = None
    viewname = None

    def get_required_permission(self):
        return 'core.view_objectchange'

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
                self.viewname, user_id=None, user_name=None, type_id=None, action=None, kind=None
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
            u['filter_url'] = self.user_filter_url(flt, u)
        contributors = 'plugins:netbox_changelog_insights:contributors'
        context.update({
            'contributors_url': flt.url(contributors),
            'person_url': flt.url(contributors, kind='person'),
            'automation_url': flt.url(contributors, kind='automation'),
            'pulse': pulse,
            'daily_chart': charts.bar_chart(pulse['daily']),
            'recent': pulse['recent'],
            'automation_ids': insights.automation_user_ids(),
        })
        return render(request, self.template_name, context)


class ContributorsView(InsightsView):
    template_name = 'netbox_changelog_insights/contributors.html'
    viewname = 'plugins:netbox_changelog_insights:contributors'

    def get(self, request):
        flt = InsightsFilter.from_request(request)
        insights = Insights(request.user, flt)
        data = insights.contributors()
        peak = max((max(u['weekly'], default=0) for u in data['users']), default=0)
        for u in data['users']:
            u['filter_url'] = self.user_filter_url(flt, u, 'plugins:netbox_changelog_insights:pulse')
            u['sparkline'] = charts.sparkline(u['weekly'], peak, data['weeks'])
        context = self.base_context(request, flt, insights)
        context.update(data)
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
