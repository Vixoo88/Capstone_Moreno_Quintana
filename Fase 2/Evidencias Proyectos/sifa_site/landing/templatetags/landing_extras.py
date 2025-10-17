from django import template
register = template.Library()

@register.filter
def get_item(d, key):
    """Devuelve d[key] si existe; string vacío en caso contrario."""
    try:
        return d.get(key, "")
    except AttributeError:
        return ""
