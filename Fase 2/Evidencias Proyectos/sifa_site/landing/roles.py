from functools import wraps
from django.core.exceptions import PermissionDenied
from django.contrib.auth.views import redirect_to_login

ADMIN_GROUP = "Admin SIFA"
CUIDADORA_GROUP = "Cuidadora"
TENS_GROUP = "TENS"

def is_admin(user):
    return user.is_authenticated and (user.is_superuser or user.groups.filter(name=ADMIN_GROUP).exists())

def is_cuidadora(user):
    return user.is_authenticated and user.groups.filter(name=CUIDADORA_GROUP).exists()

def is_tens(user):
    return user.is_authenticated and user.groups.filter(name=TENS_GROUP).exists()

def _require_role(test_func):
    def decorator(view_func):
        @wraps(view_func)
        def _wrapped(request, *args, **kwargs):
            u = request.user
            if not u.is_authenticated:
                return redirect_to_login(request.get_full_path(), login_url='login')
            if test_func(u):
                return view_func(request, *args, **kwargs)
            raise PermissionDenied
        return _wrapped
    return decorator

admin_required = _require_role(is_admin)
cuidadora_or_admin_required = _require_role(lambda u: is_admin(u) or is_cuidadora(u))
tens_or_admin_required = _require_role(lambda u: is_admin(u) or is_tens(u))
staff_view_required = _require_role(lambda u: is_admin(u) or is_cuidadora(u) or is_tens(u))
