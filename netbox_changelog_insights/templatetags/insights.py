from django import template

from core.choices import ObjectChangeActionChoices
from utilities.object_types import object_type_name

register = template.Library()


@register.filter
def type_name(object_type, include_app=True):
    """Human-friendly object type name, for example "DCIM > Interface"."""
    if object_type is None:
        return ''
    return object_type_name(object_type, include_app=include_app)


@register.filter
def action_color(action):
    return ObjectChangeActionChoices.colors.get(action, 'secondary')


@register.filter
def action_label(action):
    return dict(ObjectChangeActionChoices).get(action, action)


@register.filter
def get_item(mapping, key):
    return mapping.get(key) if mapping else None
