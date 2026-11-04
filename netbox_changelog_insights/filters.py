"""Period and dimension filter shared by all pages, and links to the core changelog."""
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from django import forms
from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from core.choices import ObjectChangeActionChoices
from netbox.plugins import get_plugin_config

__all__ = (
    'PERIODS',
    'changeset_changelog_url',
    'InsightsFilter',
    'InsightsFilterForm',
    'local_tz',
)

PERIODS = (7, 30, 90)
DEFAULT_PERIOD = 30
KIND_PERSON = 'person'
KIND_AUTOMATION = 'automation'
# site_id or tenant_id of records without a site or tenant; "null" in query strings.
NO_SCOPE = 0


def changeset_changelog_url(request_id):
    """Link to all records of one change set in the core changelog."""
    return f"{reverse('core:objectchange_list')}?{urlencode({'request_id': request_id})}"


def local_tz():
    """NetBox TIME_ZONE (design decision 8)."""
    return ZoneInfo(settings.TIME_ZONE)


class ScopeIdField(forms.IntegerField):
    """Id of a site or tenant, or "null" (as in NetBox filters): records without one, stored as 0."""

    def to_python(self, value):
        if isinstance(value, str) and value.strip().lower() == 'null':
            return NO_SCOPE
        return super().to_python(value)


class InsightsFilterForm(forms.Form):
    period = forms.ChoiceField(
        choices=[(str(p), _('{n} days').format(n=p)) for p in PERIODS] + [('custom', _('Custom'))],
        required=False,
    )
    start = forms.DateField(required=False, widget=forms.DateInput(attrs={'type': 'date'}))
    end = forms.DateField(required=False, widget=forms.DateInput(attrs={'type': 'date'}))
    user_id = forms.IntegerField(required=False, min_value=1)
    user_name = forms.CharField(required=False, max_length=150)
    type_id = forms.IntegerField(required=False, min_value=1)
    action = forms.ChoiceField(choices=[('', '')] + list(ObjectChangeActionChoices), required=False)
    kind = forms.ChoiceField(
        choices=[('', ''), (KIND_PERSON, _('Person')), (KIND_AUTOMATION, _('Automation'))],
        required=False,
    )
    site_id = ScopeIdField(required=False, min_value=NO_SCOPE)
    tenant_id = ScopeIdField(required=False, min_value=NO_SCOPE)
    onboarding = forms.BooleanField(required=False)


@dataclass
class InsightsFilter:
    """A time range [start, end) in NetBox TIME_ZONE and optional dimension filters."""
    start: datetime
    end: datetime
    period: str = str(DEFAULT_PERIOD)
    user_id: int | None = None
    user_name: str | None = None
    type_id: int | None = None
    action: str | None = None
    kind: str | None = None
    site_id: int | None = None
    tenant_id: int | None = None
    onboarding: bool = False
    errors: dict = field(default_factory=dict)
    # Ids of the users of the selected actor kind, for links to the core changelog.
    kind_user_ids: list | None = None
    # First day asked for, when the setting max_period_days cut the period; else None.
    cut_from: date | None = None

    @classmethod
    def from_request(cls, request, now=None):
        return cls.from_params(request.GET, now)

    @classmethod
    def from_params(cls, params, now=None):
        form = InsightsFilterForm(params)
        valid = form.is_valid()
        # Keep the valid fields; report and drop the others.
        data = form.cleaned_data
        tz = local_tz()
        now = (now or timezone.now()).astimezone(tz)
        today = now.date()
        period = data.get('period') or str(DEFAULT_PERIOD)
        start_day, end_day = None, None
        if period == 'custom':
            start_day = data.get('start')
            end_day = data.get('end') or today
            if start_day is None or start_day > end_day:
                period = str(DEFAULT_PERIOD)
                start_day = None
        if start_day is None:
            end_day = today
            start_day = today - timedelta(days=int(period) - 1)
        # Optional upper bound of the period: keep the last max_period_days days.
        cut_from = None
        max_days = get_plugin_config('netbox_changelog_insights', 'max_period_days')
        if max_days and (end_day - start_day).days + 1 > max_days:
            cut_from = start_day
            start_day = end_day - timedelta(days=max_days - 1)
        start = datetime.combine(start_day, time.min, tz)
        end = datetime.combine(end_day + timedelta(days=1), time.min, tz)
        return cls(
            start=start,
            end=end,
            period=period,
            user_id=data.get('user_id'),
            user_name=data.get('user_name') or None,
            type_id=data.get('type_id'),
            action=data.get('action') or None,
            kind=data.get('kind') or None,
            site_id=data.get('site_id'),
            tenant_id=data.get('tenant_id'),
            onboarding=bool(data.get('onboarding')),
            cut_from=cut_from,
            errors=dict(form.errors) if not valid else {},
        )

    # Days

    @property
    def start_date(self) -> date:
        return self.start.date()

    @property
    def end_date(self) -> date:
        """Last day in the range (inclusive)."""
        return (self.end - timedelta(days=1)).date()

    @property
    def days(self):
        return (self.end_date - self.start_date).days + 1

    # Labels

    @property
    def object_type(self):
        if self.type_id:
            return ContentType.objects.filter(pk=self.type_id).first()
        return None

    @property
    def site(self):
        return self._scope_object('dcim', 'site', self.site_id)

    @property
    def tenant(self):
        return self._scope_object('tenancy', 'tenant', self.tenant_id)

    @staticmethod
    def _scope_object(app, model, pk):
        """Name of a site or tenant; "#id" when it does not exist any more."""
        if pk is None:
            return None
        if pk == NO_SCOPE:
            return str(_('None'))
        ct = ContentType.objects.get_by_natural_key(app, model)
        obj = ct.model_class().objects.filter(pk=pk).first()
        return str(obj) if obj is not None else f'#{pk}'

    @property
    def is_filtered(self):
        return any((self.user_id, self.user_name, self.type_id, self.action, self.kind)) or any(
            v is not None for v in (self.site_id, self.tenant_id))

    # Query strings

    def params(self, **override):
        """Plugin query parameters for this filter, with overrides; None removes a key."""
        p = {'period': self.period}
        if self.period == 'custom':
            p['start'] = self.start_date.isoformat()
            p['end'] = self.end_date.isoformat()
        for key in ('user_id', 'user_name', 'type_id', 'action', 'kind'):
            if getattr(self, key):
                p[key] = getattr(self, key)
        for key in ('site_id', 'tenant_id'):
            value = getattr(self, key)
            if value is not None:
                p[key] = 'null' if value == NO_SCOPE else value
        if self.onboarding:
            p['onboarding'] = 1
        for key, value in override.items():
            if value is None:
                p.pop(key, None)
            else:
                p[key] = value
        return p

    def url(self, viewname, **override):
        return f'{reverse(viewname)}?{urlencode(self.params(**override))}'

    def changelog_url(self, user_ids=None, **extra):
        """
        Link to the core changelog list with the same filter (design decision 6).
        extra: core filter parameters (user_id, user_name, action, changed_object_type_id,
        request_id, changed_object_id) that refine this filter. user_ids: list of user ids,
        for a filter by actor kind; default: kind_user_ids. The core changelog cannot
        select deleted users by kind, and cannot filter by site or tenant: the link drops
        that filter.
        """
        if user_ids is None and self.kind:
            user_ids = self.kind_user_ids
        fmt = '%Y-%m-%d %H:%M:%S'
        p = [
            ('time_after', self.start.strftime(fmt)),
            ('time_before', (self.end - timedelta(seconds=1)).strftime(fmt)),
        ]
        values = {
            'user_id': self.user_id,
            'user_name': None if self.user_id else self.user_name,
            'changed_object_type_id': self.type_id,
            'action': self.action,
        }
        values.update(extra)
        if user_ids is not None and not values.get('user_id') and not values.get('user_name'):
            p.extend(('user_id', uid) for uid in user_ids)
        for key, value in values.items():
            if value not in (None, ''):
                p.append((key, value))
        return f"{reverse('core:objectchange_list')}?{urlencode(p)}"
