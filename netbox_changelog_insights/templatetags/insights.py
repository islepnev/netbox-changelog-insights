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
