"""Labels for object types. Plain strings, so that cached values hold no model instances."""

__all__ = (
    'type_label',
)


def _title(value):
    """Capitalize the first letter of each word; keep the other letters (DCIM stays DCIM)."""
    return ' '.join(w[:1].upper() + w[1:] for w in str(value).split(' '))


def type_label(object_type, include_app=True):
    """Human-friendly name of a ContentType, for example "DCIM > Interface"."""
    if object_type is None:
        return ''
    model = object_type.model_class()
    if model is None:
        # The model is not installed any more (for example a removed plugin).
        name, app = object_type.model, object_type.app_label
    else:
        name, app = _title(model._meta.verbose_name), _title(model._meta.app_config.verbose_name)
    return f'{app} > {name}' if include_app else name
