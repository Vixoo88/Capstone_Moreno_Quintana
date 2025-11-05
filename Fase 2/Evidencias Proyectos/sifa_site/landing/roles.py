# landing/roles.py
from functools import wraps
from django.contrib.auth.models import Group
from django.http import HttpResponseForbidden
from django.shortcuts import redirect
from django.contrib import messages

ADMIN_GROUP = "ADMIN"
TENS_GROUP = "TENS"
CUIDADORA_GROUP = "CUIDADORA"
DOCTOR_GROUP = "DOCTOR"  # << nuevo

def _in_group(user, name):
    return user.is_authenticated and user.groups.filter(name=name).exists()

def is_admin(user): return _in_group(user, ADMIN_GROUP) or user.is_superuser
def is_tens(user): return _in_group(user, TENS_GROUP)
def is_cuidadora(user): return _in_group(user, CUIDADORA_GROUP)
def is_doctor(user): return _in_group(user, DOCTOR_GROUP)  # << nuevo

def _deny(request):
    messages.error(request, "No tienes permisos para esta acción.")
    return redirect("dashboard")

def admin_required(view):
    @wraps(view)
    def _w(request, *a, **kw):
        return view(request, *a, **kw) if is_admin(request.user) else _deny(request)
    return _w

def tens_or_admin_required(view):
    @wraps(view)
    def _w(request, *a, **kw):
        u = request.user
        return view(request, *a, **kw) if (is_admin(u) or is_tens(u)) else _deny(request)
    return _w

def cuidadora_or_admin_required(view):
    @wraps(view)
    def _w(request, *a, **kw):
        u = request.user
        return view(request, *a, **kw) if (is_admin(u) or is_cuidadora(u)) else _deny(request)
    return _w

# Vista “de staff”: dashboard, listados básicos…
def staff_view_required(view):
    @wraps(view)
    def _w(request, *a, **kw):
        u = request.user
        return view(request, *a, **kw) if (is_admin(u) or is_tens(u) or is_cuidadora(u) or is_doctor(u)) else _deny(request)
    return _w

# << nuevo: para permitir acciones a DOCTOR o ADMIN
def doctor_or_admin_required(view):
    @wraps(view)
    def _w(request, *a, **kw):
        u = request.user
        return view(request, *a, **kw) if (is_admin(u) or is_doctor(u)) else _deny(request)
    return _w

# Nuevo: permite ver residentes a ADMIN, DOCTOR y TENS
def doctor_tens_or_admin_required(view):
    @wraps(view)
    def _wrapped(request, *args, **kwargs):
        u = request.user
        if is_admin(u) or is_doctor(u) or is_tens(u):
            return view(request, *args, **kwargs)
        return _deny(request)
    return _wrapped