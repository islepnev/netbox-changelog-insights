from django import template

from core.choices import ObjectChangeActionChoices

from ..labels import type_label

register = template.Library()


@register.filter
def type_name(object_type, include_app=True):
    """Human-friendly object type name, for example "DCIM > Interface"."""
    return type_label(object_type, include_app=include_app)


@register.filter
def action_color(action):
    return ObjectChangeActionChoices.colors.get(action, 'secondary')


@register.filter
def action_label(action):
    return dict(ObjectChangeActionChoices).get(action, action)


@register.filter
def duration(seconds):
    """Seconds as a short duration: 45 s, 12 min, 4.4 h, 6.3 d."""
    if seconds is None:
        return ''
    seconds = float(seconds)
    if seconds < 60:
        return f'{seconds:.0f} s'
    if seconds < 3600:
        return f'{seconds / 60:.0f} min'
    if seconds < 86400:
        return f'{seconds / 3600:.1f} h'
    return f'{seconds / 86400:.1f} d'


STAGE_COLORS = {
    'create': 'green', 'delete': 'red', 'components': 'teal', 'modules': 'cyan', 'cabling': 'purple',
    'metadata': 'blue', 'component edits': 'azure', 'assets': 'orange', 'ip': 'indigo', 'contacts': 'pink',
}


@register.filter
def stage_color(stage):
    return STAGE_COLORS.get(stage, 'secondary')
