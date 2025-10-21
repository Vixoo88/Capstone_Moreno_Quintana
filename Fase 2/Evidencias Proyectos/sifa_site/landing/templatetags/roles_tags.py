from django import template
from landing.roles import ADMIN_GROUP, is_admin as _is_admin, is_cuidadora as _is_cuidadora, is_tens as _is_tens

register = template.Library()

@register.filter
def has_group(user, group_name):
    if not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_superuser", False) and group_name == ADMIN_GROUP:
        return True
    return user.groups.filter(name=group_name).exists()

@register.filter(name='is_admin')
def is_admin_filter(user):
    return _is_admin(user)

@register.filter(name='is_cuidadora')
def is_cuidadora_filter(user):
    return _is_cuidadora(user)

@register.filter(name='is_tens')
def is_tens_filter(user):
    return _is_tens(user)
