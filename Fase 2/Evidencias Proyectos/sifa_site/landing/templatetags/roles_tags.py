from django import template

# ajusta el import si tu app NO se llama "landing"
from landing import roles as rolelib

register = template.Library()

# --- Filtros de rol visibles en templates ---
@register.filter(name="is_admin")
def is_admin_filter(user):
    return rolelib.is_admin(user)

@register.filter(name="is_tens")
def is_tens_filter(user):
    return rolelib.is_tens(user)

@register.filter(name="is_cuidadora")
def is_cuidadora_filter(user):
    return rolelib.is_cuidadora(user)

@register.filter(name="is_doctor")
def is_doctor_filter(user):
    return rolelib.is_doctor(user)

# --- Utilidades opcionales que ya usamos antes ---
@register.filter
def has_group(user, name: str):
    """Devuelve True si el usuario pertenece al grupo exacto `name`."""
    return user.is_authenticated and user.groups.filter(name=name).exists()

@register.filter
def has_any_group(user, csv_names: str):
    """True si el usuario pertenece a cualquiera de los grupos dados por coma."""
    if not user.is_authenticated:
        return False
    wanted = {n.strip() for n in (csv_names or "").split(",") if n.strip()}
    return user.groups.filter(name__in=wanted).exists() or user.is_superuser

@register.filter
def can_view_residentes(user):
    """ADMIN, DOCTOR o TENS pueden ver residentes."""
    return rolelib.is_admin(user) or rolelib.is_doctor(user) or rolelib.is_tens(user)
