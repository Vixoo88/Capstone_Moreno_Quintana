# landing/views.py
from collections import defaultdict
from calendar import monthrange
from datetime import datetime, time as dtime

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import logout
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Q, Prefetch, F
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST
from django.core.cache import cache
from .notifications import send_telegram_message
import random
from django.contrib.auth.models import User
from django.views.decorators.http import require_http_methods
from .models import Asignacion, DiaAsignacion
# arriba en views.py
from .roles import (
    admin_required, cuidadora_or_admin_required, tens_or_admin_required, staff_view_required,
    is_cuidadora, is_tens, is_admin,
    CUIDADORA_GROUP,
)

from .models import (
    Administracion, HoraProgramada, OrdenMedicamento, Receta, Residente, Producto
)
from .forms import (
    AdminMarcarForm, OrdenMedicamentoForm, ProductoQuickForm,
    RecetaForm, ResidenteForm
)

# --------- imports opcionales para APIs externas ---------
try:
    import requests  # usado por CIMA/RxNorm; si falta, la API externa se omite
except Exception:
    requests = None


# =========================================================
# Helpers
# =========================================================

def _parse_horas_from_post(request):
    """Lee name='hora[]' y name='dia[]' y devuelve [{'hora': dtime, 'dia': int|None}, ...]."""
    horas = request.POST.getlist('hora[]')
    dias = request.POST.getlist('dia[]')
    out = []
    for i, h in enumerate(horas):
        h = (h or '').strip()
        if not h:
            continue
        try:
            hh, mm = h.split(':', 1)
            t = dtime(int(hh), int(mm))
        except Exception:
            continue
        d_raw = (dias[i] if i < len(dias) else '').strip()
        if d_raw == '' or d_raw is None:
            d = None
        else:
            try:
                d = int(d_raw)
            except Exception:
                d = None
        out.append({'hora': t, 'dia': d})
    return out

def _fmt_ampm(dt_aware):
    """‘10 am’, ‘10:30 pm’ en hora local."""
    s = timezone.localtime(dt_aware).strftime("%I:%M %p").lower()
    if s.startswith("0"):
        s = s[1:]
    return s.replace(".", "")

def _short_user(u):
    """Nombre corto del usuario."""
    if not u:
        return ''
    name = (u.get_full_name() or u.username).strip()
    return name.split()[0] if name else u.username

def _ajustar_stock_por_transicion(evento, old, new):
    """
    Si pasa a DADA, descuenta 1 del stock_asignado.
    Si sale de DADA a otro estado, repone 1.
    """
    orden = evento.orden
    if not hasattr(orden, 'stock_asignado'):
        return
    if old != 'DADA' and new == 'DADA':
        if orden.stock_asignado > 0:
            orden.stock_asignado -= 1
            orden.save(update_fields=['stock_asignado'])
    elif old == 'DADA' and new != 'DADA':
        orden.stock_asignado += 1
        orden.save(update_fields=['stock_asignado'])

    _check_alerta_stock(orden)

def _check_alerta_stock(orden):
    """
    Si stock_asignado <= stock_critico y no se ha avisado, envía Telegram y marca alerta_enviada=True.
    Si sale de crítico (> stock_critico) y estaba marcada, resetea alerta_enviada=False.
    """
    try:
        critico = (orden.stock_asignado or 0) <= (orden.stock_critico or 0)
        if critico and not orden.alerta_enviada:
            res = orden.receta.residente
            msg = (
                "⚠️ <b>Stock crítico</b>\n"
                f"👤 Residente: {res.nombre_completo}\n"
                f"💊 Medicamento: {orden.producto} · {orden.dosis}\n"
                f"📦 Stock: {orden.stock_asignado} (crítico {orden.stock_critico})"
            )
            ok = send_telegram_message(msg)
            if ok:
                orden.alerta_enviada = True
                orden.save(update_fields=['alerta_enviada'])
        elif not critico and orden.alerta_enviada:
            # se repone → listo para volver a avisar la próxima vez
            orden.alerta_enviada = False
            orden.save(update_fields=['alerta_enviada'])
    except Exception:
        # No romper el flujo si hay un problema de red
        pass


# =========================================================
# Público / Auth / Dashboard
# =========================================================

def home_public(request):
    if request.user.is_authenticated:
        return redirect('dashboard')
    return render(request, 'landing/home.html')

@login_required
@staff_view_required
def dashboard(request):
    hoy = timezone.localdate()
    admins_hoy = Administracion.objects.filter(programada_para__date=hoy).count()

    # Alertas de stock crítico
    criticos = (
        OrdenMedicamento.objects
        .select_related('receta__residente', 'producto')
        .filter(activo=True, stock_asignado__lte=F('stock_critico'))
        .order_by('receta__residente__nombre_completo')
    )

    return render(request, 'landing/dashboard.html', {
        'admins_hoy': admins_hoy,
        'criticos': criticos,
    })

@login_required
@tens_or_admin_required
def orden_restock(request, orden_id):
    orden = get_object_or_404(OrdenMedicamento, pk=orden_id)
    if request.method == 'POST':
        try:
            sumar = int(request.POST.get('sumar', '0'))
        except ValueError:
            sumar = 0
        if sumar > 0:
            orden.stock_asignado = (orden.stock_asignado or 0) + sumar
            orden.save(update_fields=['stock_asignado'])
            _check_alerta_stock(orden)
            messages.success(request, f'Se añadieron {sumar} al stock de {orden}.')
        else:
            messages.error(request, 'Cantidad inválida.')
    return redirect('dashboard')

@require_POST
def logout_view(request):
    logout(request)
    messages.success(request, "Sesión cerrada. ¡Hasta luego!")
    return redirect('home_public')


# =========================================================
# Residentes
# =========================================================

@login_required
@tens_or_admin_required
def residente_list(request):
    qs = Residente.objects.filter(activo=True).order_by('nombre_completo')
    return render(request, 'residentes/residentes_list.html', {'residentes': qs})

@login_required
@admin_required
def residente_create(request):
    form = ResidenteForm(request.POST or None)
    if request.method == 'POST':
        if form.is_valid():
            r = form.save()
            messages.success(request, 'Residente creado. Ahora registra una receta.')
            return redirect('receta_create', residente_id=r.id)
        messages.error(request, 'No se pudo guardar el residente. Revisa los campos.')
    return render(request, 'residentes/residente_form.html', {'form': form})

@login_required
@admin_required
def residente_detail(request, residente_id):
    res = get_object_or_404(
        Residente.objects.prefetch_related(
            Prefetch(
                'recetas',
                queryset=Receta.objects.order_by('-creada_en').prefetch_related(
                    Prefetch(
                        'ordenes',
                        queryset=OrdenMedicamento.objects.prefetch_related('horas', 'producto')
                    )
                )
            )
        ),
        pk=residente_id
    )
    return render(request, 'residentes/residente_detail.html', {'residente': res})

@login_required
@transaction.atomic
@admin_required
def residente_delete(request, residente_id):
    """
    Elimina al residente y todo su historial en orden seguro (evita ProtectedError):
    1) Administraciones del residente
    2) Recetas del residente (borra órdenes y horas por CASCADE)
    3) Residente
    """
    residente = get_object_or_404(Residente, pk=residente_id)

    if request.method == 'POST':
        nombre = residente.nombre_completo
        Administracion.objects.filter(residente=residente).delete()
        Receta.objects.filter(residente=residente).delete()
        residente.delete()
        messages.success(request, f'Se eliminó a "{nombre}" y todo su historial.')
        return redirect('residente_list')

    return render(request, 'residentes/confirm_delete.html', {
        'titulo': 'Eliminar residente',
        'detalle': f'Se eliminará al residente "{residente.nombre_completo}" y todo su historial '
                   '(recetas, medicamentos, horas y registros de administración).',
        'post_url': request.path,
        'volver_url': 'residente_detail',
        'volver_args': [residente.id],
    })


# =========================================================
# Recetas / Órdenes
# =========================================================

@login_required
@transaction.atomic
@admin_required
def receta_create(request, residente_id):
    res = get_object_or_404(Residente, pk=residente_id)
    receta_form = RecetaForm(request.POST or None)
    orden_form = OrdenMedicamentoForm(request.POST or None)
    producto_form = ProductoQuickForm(request.POST or None, prefix='prod')

    horas_data = _parse_horas_from_post(request) if request.method == 'POST' else []
    error_horas = error_producto = None

    if request.method == 'POST':
        base_ok = receta_form.is_valid() and orden_form.is_valid() and producto_form.is_valid()
        if not horas_data:
            error_horas = "Agrega al menos una hora."
        prod_sel = orden_form.cleaned_data.get('producto') if orden_form.is_valid() else None
        creando_nuevo = bool(producto_form.cleaned_data.get('nombre')) if producto_form.is_valid() else False
        if not prod_sel and not creando_nuevo:
            error_producto = "Selecciona un medicamento o completa 'Nuevo medicamento'."

        if base_ok and not error_horas and not error_producto:
            receta = receta_form.save(commit=False)
            receta.residente = res
            receta.medico = request.user
            receta.save()

            producto = prod_sel or producto_form.create_if_filled()

            orden = orden_form.save(commit=False)
            orden.receta = receta
            orden.producto = producto
            orden.save()
            _check_alerta_stock(orden)

            for item in horas_data:
                HoraProgramada.objects.create(
                    orden=orden, hora=item['hora'], dia_semana=item['dia']
                )

            messages.success(request, 'Receta creada correctamente.')
            return redirect('residente_detail', residente_id=res.id)

        messages.error(request, 'No se pudo guardar la receta. Revisa los errores.')

    return render(request, 'recetas/receta_form.html', {
        'residente': res,
        'receta_form': receta_form,
        'orden_form': orden_form,
        'producto_form': producto_form,
        'error_horas': error_horas,
        'error_producto': error_producto,
        'horas_data': horas_data,
        'modo_edicion': False,
    })

@login_required
@admin_required
def receta_delete(request, receta_id):
    receta = get_object_or_404(Receta, pk=receta_id)
    res_id = receta.residente_id
    if request.method == 'POST':
        receta.delete()
        messages.success(request, 'Receta eliminada.')
        return redirect('residente_detail', residente_id=res_id)

    return render(request, 'residentes/confirm_delete.html', {
        'titulo': 'Eliminar receta',
        'detalle': f'Receta #{receta.id} del residente {receta.residente}',
        'post_url': request.path,
        'volver_url': 'residente_detail',
        'volver_args': [res_id],
    })

@login_required
@transaction.atomic
@admin_required
def orden_create(request, receta_id):
    receta = get_object_or_404(Receta, pk=receta_id)
    orden_form = OrdenMedicamentoForm(request.POST or None)
    producto_form = ProductoQuickForm(request.POST or None, prefix='prod')

    horas_data = _parse_horas_from_post(request) if request.method == 'POST' else []
    error_horas = error_producto = None

    if request.method == 'POST':
        base_ok = orden_form.is_valid() and producto_form.is_valid()
        if not horas_data:
            error_horas = "Agrega al menos una hora."
        prod_sel = orden_form.cleaned_data.get('producto') if orden_form.is_valid() else None
        creando_nuevo = bool(producto_form.cleaned_data.get('nombre')) if producto_form.is_valid() else False
        if not prod_sel and not creando_nuevo:
            error_producto = "Selecciona un medicamento o completa 'Nuevo medicamento'."

        if base_ok and not error_horas and not error_producto:
            producto = prod_sel or producto_form.create_if_filled()
            orden = orden_form.save(commit=False)
            orden.receta = receta
            orden.producto = producto
            orden.save()
            _check_alerta_stock(orden)

            for item in horas_data:
                HoraProgramada.objects.create(
                    orden=orden, hora=item['hora'], dia_semana=item['dia']
                )

            messages.success(request, 'Medicamento agregado a la receta.')
            return redirect('residente_detail', residente_id=receta.residente_id)

        messages.error(request, 'No se pudo agregar el medicamento. Revisa los errores.')

    return render(request, 'ordenes/orden_form.html', {
        'receta': receta,
        'orden_form': orden_form,
        'producto_form': producto_form,
        'error_horas': error_horas,
        'error_producto': error_producto,
        'horas_data': horas_data,
        'modo_edicion': False,
    })

@login_required
@transaction.atomic
@admin_required
def orden_edit(request, orden_id):
    orden = get_object_or_404(OrdenMedicamento.objects.select_related('receta', 'producto'), pk=orden_id)
    orden_form = OrdenMedicamentoForm(request.POST or None, instance=orden)
    producto_form = ProductoQuickForm(request.POST or None, prefix='prod')

    horas_data = _parse_horas_from_post(request) if request.method == 'POST' else [
        {'hora': h.hora, 'dia': h.dia_semana} for h in orden.horas.all()
    ]
    error_horas = error_producto = None

    if request.method == 'POST':
        base_ok = orden_form.is_valid() and producto_form.is_valid()
        if not horas_data:
            error_horas = "Agrega al menos una hora."
        prod_sel = orden_form.cleaned_data.get('producto') if orden_form.is_valid() else None
        creando_nuevo = bool(producto_form.cleaned_data.get('nombre')) if producto_form.is_valid() else False
        if not prod_sel and not creando_nuevo and not orden.producto_id:
            error_producto = "Selecciona un medicamento o completa 'Nuevo medicamento'."

        if base_ok and not error_horas and not error_producto:
            if creando_nuevo and not prod_sel:
                orden.producto = producto_form.create_if_filled()
            orden = orden_form.save()

            # Reemplazar horas
            orden.horas.all().delete()
            for item in horas_data:
                HoraProgramada.objects.create(
                    orden=orden, hora=item['hora'], dia_semana=item['dia']
                )

            messages.success(request, 'Medicamento actualizado.')
            return redirect('residente_detail', residente_id=orden.receta.residente_id)

        messages.error(request, 'No se pudo guardar cambios. Revisa los errores.')

    return render(request, 'ordenes/orden_form.html', {
        'receta': orden.receta,
        'orden': orden,
        'orden_form': orden_form,
        'producto_form': producto_form,
        'error_horas': error_horas,
        'error_producto': error_producto,
        'horas_data': horas_data,
        'modo_edicion': True,
    })

@login_required
@admin_required
def orden_delete(request, orden_id):
    orden = get_object_or_404(OrdenMedicamento, pk=orden_id)
    res_id = orden.receta.residente_id
    if request.method == 'POST':
        orden.delete()
        messages.success(request, 'Medicamento eliminado.')
        return redirect('residente_detail', residente_id=res_id)
    return render(request, 'residentes/confirm_delete.html', {
        'titulo': 'Eliminar medicamento',
        'detalle': f'{orden.producto} — {orden.dosis}',
        'post_url': request.path,
        'volver_url': 'residente_detail',
        'volver_args': [res_id],
    })


# =========================================================
# Administración (Hoy)
# =========================================================

def _generar_eventos_hoy():
    """Genera registros PENDIENTE para hoy (hora local) según recetas activas."""
    hoy = timezone.localdate()
    dia_sem = hoy.weekday()
    tz = timezone.get_current_timezone()

    recetas = (
        Receta.objects.filter(activa=True, inicio__lte=hoy)
        .filter(Q(fin__isnull=True) | Q(fin__gte=hoy))
    )
    ordenes = (
        OrdenMedicamento.objects.filter(receta__in=recetas, activo=True)
        .prefetch_related('horas', 'receta__residente')
    )
    for orden in ordenes:
        for h in orden.horas.all():
            if h.dia_semana is not None and h.dia_semana != dia_sem:
                continue
            dt_local = timezone.make_aware(datetime.combine(hoy, h.hora), tz)
            Administracion.objects.get_or_create(
                orden=orden,
                residente=orden.receta.residente,
                programada_para=dt_local,
                defaults={'estado': Administracion.Estado.PENDIENTE},
            )

@login_required
@staff_view_required
def admin_list_hoy(request):
    _generar_eventos_hoy()
    hoy = timezone.localdate()

    eventos_qs = (
        Administracion.objects.select_related("orden__producto", "residente", "realizada_por")
        .filter(programada_para__date=hoy)
    )

    # Modo del día
    modo = DiaAsignacion.objects.filter(fecha=hoy).first()
    if modo and modo.solo_asignados and is_cuidadora(request.user):
        # residentes asignados a esta cuidadora hoy
        res_ids = list(Asignacion.objects.filter(fecha=hoy, cuidadora=request.user).values_list('residente_id', flat=True))
        eventos_qs = eventos_qs.filter(residente_id__in=res_ids)

    # (resto de la agrupación por horas igual que ya tenías)
    buckets = defaultdict(list)
    counts = defaultdict(int)
    for e in eventos_qs:
        hhmm = timezone.localtime(e.programada_para).strftime("%H:%M")
        buckets[hhmm].append(e)
        counts[hhmm] += 1

    def keyf(h): return (int(h[:2]), int(h[3:5]))
    horas_sorted = sorted(counts.keys(), key=keyf)

    selected = request.GET.get('h')
    if selected:
        grupos = {selected: sorted(buckets.get(selected, []),
                                   key=lambda x: x.residente.nombre_completo)}
    else:
        grupos = {h: sorted(buckets[h], key=lambda x: x.residente.nombre_completo)
                  for h in horas_sorted}

    horas = [(h, counts[h]) for h in horas_sorted]

    return render(request, 'administracion/admin_hoy.html', {
        'grupos': grupos,
        'hoy': hoy,
        'horas': horas,
        'seleccion': selected,
    })

@login_required
@cuidadora_or_admin_required
def admin_marcar_rapido(request, admin_id):
    """Marca una administración con un clic y ajusta stock."""
    if request.method != 'POST':
        return redirect('admin_list_hoy')
    evento = get_object_or_404(Administracion, pk=admin_id)
    new = request.POST.get('estado')
    if new not in ('DADA', 'OMITIDA', 'RECHAZADA', 'PENDIENTE'):
        messages.error(request, 'Estado inválido.')
        return redirect('admin_list_hoy')

    old = evento.estado
    evento.estado = new
    evento.realizada_por = request.user
    evento.save(update_fields=['estado', 'realizada_por'])
    _ajustar_stock_por_transicion(evento, old, new)

    h = request.GET.get('h')
    url = reverse('admin_list_hoy') + (f'?h={h}' if h else '')
    return redirect(url)

@login_required
@cuidadora_or_admin_required
def admin_marcar_grupo(request):
    """Marca todos los eventos de una hora (hoy) con el mismo estado; ajusta stock."""
    if request.method != 'POST':
        return redirect('admin_list_hoy')

    hora = request.POST.get('hora')  # 'HH:MM'
    new = request.POST.get('estado')
    if not hora or new not in ('DADA', 'OMITIDA', 'RECHAZADA', 'PENDIENTE'):
        messages.error(request, 'Datos inválidos.')
        return redirect('admin_list_hoy')

    hoy = timezone.localdate()
    eventos = (
        Administracion.objects.select_related("residente", "orden")
        .filter(programada_para__date=hoy)
    )

    updated = 0
    for e in eventos:
        hhmm = timezone.localtime(e.programada_para).strftime("%H:%M")
        if hhmm == hora:
            old = e.estado
            e.estado = new
            e.realizada_por = request.user
            e.save(update_fields=['estado', 'realizada_por'])
            _ajustar_stock_por_transicion(e, old, new)
            updated += 1

    messages.success(request, f'{updated} registros marcados como {new.lower()}.')
    return redirect(reverse('admin_list_hoy') + f'?h={hora}')

@login_required
@cuidadora_or_admin_required
def admin_marcar(request, admin_id):
    """Página de marcado detallado (opcional)."""
    evento = get_object_or_404(Administracion, pk=admin_id)
    old = evento.estado
    form = AdminMarcarForm(request.POST or None, instance=evento)
    if request.method == 'POST' and form.is_valid():
        e = form.save(commit=False)
        e.realizada_por = request.user
        e.save()
        _ajustar_stock_por_transicion(e, old, e.estado)
        messages.success(request, 'Registro actualizado.')
        return redirect('admin_list_hoy')
    return render(request, 'administracion/admin_marcar.html', {'evento': evento, 'form': form})


# =========================================================
# Registro mensual
# =========================================================

@login_required
@admin_required
def registro_mensual(request, residente_id):
    """
    Una fila por (orden, hora local). En cada celda:
    ✓ (Ana), ✕ (Pedro), R (Luis) o • si está pendiente.
    Si hubiera más de un registro ese día para la misma fila, se concatena con '/'.
    """
    res = get_object_or_404(Residente, pk=residente_id)
    hoy_local = timezone.localdate()
    y = int(request.GET.get('year', hoy_local.year))
    m = int(request.GET.get('month', hoy_local.month))
    days_in_month = monthrange(y, m)[1]

    inicio = timezone.make_aware(datetime(y, m, 1, 0, 0))
    fin = timezone.make_aware(datetime(y, m, days_in_month, 23, 59))

    eventos = (
        Administracion.objects
        .select_related("orden__producto", "realizada_por")
        .filter(residente=res, programada_para__range=(inicio, fin))
        .order_by("orden_id", "programada_para")
    )

    # key = (orden_id, HH:MM local)  → una fila por hora específica
    rows_map = {}
    for e in eventos:
        local_dt = timezone.localtime(e.programada_para)
        hhmm_24 = local_dt.strftime("%H:%M")
        label_hora = _fmt_ampm(e.programada_para)

        prod = e.orden.producto
        med_label = f"{prod.nombre} {prod.potencia}".strip()
        orden_label = f"{med_label} · {e.orden.dosis} — {label_hora}"

        key = (e.orden_id, hhmm_24)
        if key not in rows_map:
            rows_map[key] = {"label": orden_label, "cells": [""] * days_in_month}

        sym = {"DADA": "✓", "OMITIDA": "✕", "RECHAZADA": "R", "PENDIENTE": "•"}[e.estado]
        who = _short_user(e.realizada_por) if e.estado != 'PENDIENTE' and e.realizada_por else ''
        mark = f"{sym}{f' ({who})' if who else ''}"

        idx = local_dt.day - 1
        rows_map[key]["cells"][idx] = (rows_map[key]["cells"][idx] + " / " if rows_map[key]["cells"][idx] else "") + mark

    # Orden: por nombre de medicamento y hora
    def sort_key(item):
        (orden_id, hhmm), data = item
        h, m = int(hhmm[:2]), int(hhmm[3:5])
        return (data["label"].split(" — ")[0].lower(), h, m)

    rows = [v for _, v in sorted(rows_map.items(), key=sort_key)]

    return render(request, 'residentes/registro_mensual.html', {
        'residente': res,
        'year': y,
        'month': m,
        'days': range(1, days_in_month + 1),
        'rows': rows
    })


# =========================================================
# API Sugerencias de Medicamentos (Local + Externas opcionales)
# =========================================================

def _suggest_local(q, limit):
    qs = (Producto.objects
          .filter(Q(nombre__icontains=q) | Q(potencia__icontains=q))
          .order_by('nombre', 'potencia')[:limit])
    return [{
        "id": p.id,
        "label": f"{p.nombre} {p.potencia}".strip(),
        "source": "local",
        "nombre": p.nombre,
        "potencia": p.potencia or "",
        "forma": p.forma or "",
    } for p in qs]

# ---------- Proveedor: CIMA (AEMPS, ES) ----------
def _suggest_cima(q, limit, timeout):
    """
    Usa /medicamentos?nombre=<q> (paginado). Si no hay resultados,
    intenta /vmpp?nombre=<q> como fallback.
    """
    if not requests:
        return []

    headers = {"Accept": "application/json"}

    # 1) /medicamentos?nombre=...  (¡OJO: plural!)
    try:
        r = requests.get(
            "https://cima.aemps.es/cima/rest/medicamentos",
            params={"nombre": q, "autorizados": 1, "pagina": 1},
            timeout=timeout,
            headers=headers,
        )
        items = []
        if r.status_code == 200:
            data = r.json() or []
            # a veces viene como dict con 'resultados', otras como lista
            if isinstance(data, dict):
                items = data.get("resultados", []) or []
            elif isinstance(data, list):
                items = data

        out, seen = [], set()
        for it in items:
            # en este listado suelen venir 'nombre' y 'nregistro'
            nombre = (it.get("nombre") or "").strip()
            nreg   = it.get("nregistro") or ""
            if not nombre or nombre in seen:
                continue
            seen.add(nombre)
            out.append({
                "id": f"cima:{nreg}",
                "label": nombre,
                "source": "cima",
                "nombre": nombre,
                "potencia": "",
                "forma": "",
            })
            if len(out) >= limit:
                return out
        if out:
            return out
    except Exception:
        pass

    # 2) /vmpp?nombre=... (fallback por descripción clínica)
    try:
        r2 = requests.get(
            "https://cima.aemps.es/cima/rest/vmpp",
            params={"nombre": q, "pagina": 1},
            timeout=timeout,
            headers=headers,
        )
        if r2.status_code != 200:
            return []
        data2 = r2.json() or []
        items2 = data2.get("resultados", []) if isinstance(data2, dict) else data2
        out2, seen2 = [], set()
        for it in items2:
            nombre = (it.get("vmppDesc") or it.get("vmpDesc") or it.get("nombre") or "").strip()
            _id    = it.get("id") or it.get("vmpp") or it.get("vmp") or ""
            if not nombre or nombre in seen2:
                continue
            seen2.add(nombre)
            out2.append({
                "id": f"cima-vmpp:{_id}",
                "label": nombre,
                "source": "cima",
                "nombre": nombre,
                "potencia": "",
                "forma": "",
            })
            if len(out2) >= limit:
                break
        return out2
    except Exception:
        return []

def _suggest_rxnorm(q, limit, timeout):
    if not requests:
        return []
    # RxNorm: /REST/drugs.json?name=<q>
    url = "https://rxnav.nlm.nih.gov/REST/drugs.json"
    try:
        r = requests.get(url, params={"name": q}, timeout=timeout, headers={"Accept": "application/json"})
        if r.status_code != 200:
            return []
        data = r.json() or {}
        props = []
        for group in (data.get("drugGroup", {}) or {}).get("conceptGroup", []) or []:
            props += group.get("conceptProperties", []) or []
        seen, out = set(), []
        for p in props:
            label = p.get("name")
            rxcui = p.get("rxcui")
            if not label or label in seen:
                continue
            seen.add(label)
            out.append({
                "id": f"rxnorm:{rxcui}",
                "label": label,
                "source": "rxnorm",
                "nombre": label,
                "potencia": "",
                "forma": "",
            })
            if len(out) >= limit:
                break
        return out
    except Exception:
        return []

@login_required
def api_productos_suggest(request):
    """
    Sugerencias de medicamentos:
    - LOCAL: busca en Producto
    - CIMA: AEMPS (ES) por nombre
    - RXNORM: NIH por nombre
    - HYBRID: combina (Local primero, luego externos)
    Forzar proveedor con ?provider=LOCAL|CIMA|RXNORM|HYBRID
    """
    q = (request.GET.get('q') or '').strip()
    if not q:
        return JsonResponse({"results": []})

    provider = request.GET.get("provider") or getattr(settings, "DRUG_SUGGEST_PROVIDER", "HYBRID")
    limit    = int(getattr(settings, "DRUG_SUGGEST_LIMIT", 10))
    timeout  = int(getattr(settings, "DRUG_SUGGEST_TIMEOUT", 2))

    results, labels = [], set()

    def add(items):
        for it in items:
            if it["label"] not in labels:
                labels.add(it["label"])
                results.append(it)
                if len(results) >= limit:
                    break

    if provider in ("LOCAL", "HYBRID"):
        add(_suggest_local(q, limit))
    if len(results) < limit and provider in ("CIMA", "HYBRID"):
        add(_suggest_cima(q, limit, timeout))
    if len(results) < limit and provider in ("RXNORM", "HYBRID"):
        add(_suggest_rxnorm(q, limit, timeout))

    return JsonResponse({"results": results})

@login_required
@tens_or_admin_required
def asignaciones_hoy(request):
    hoy = timezone.localdate()

    # Todas las cuidadoras activas (grupo)
    cuidadoras = User.objects.filter(is_active=True, groups__name=CUIDADORA_GROUP).order_by('first_name', 'username')

    # Modo/selección del día
    modo, _ = DiaAsignacion.objects.get_or_create(fecha=hoy, defaults={'solo_asignados': False})
    seleccion_ids = set(modo.cuidadoras.values_list('id', flat=True))
    # Si no hay selección guardada, por UX pre-marcamos todas
    if not seleccion_ids:
        seleccion_ids = set(cuidadoras.values_list('id', flat=True))

    # Asignaciones existentes hoy
    asignaciones = (
        Asignacion.objects
        .select_related('cuidadora', 'residente')
        .filter(fecha=hoy)
        .order_by('cuidadora__username', 'residente__nombre_completo')
    )
    grupos = {}
    for a in asignaciones:
        grupos.setdefault(a.cuidadora, []).append(a.residente)

    return render(request, 'asignaciones/asignaciones_hoy.html', {
        'fecha': hoy,
        'cuidadoras': cuidadoras,
        'seleccion_ids': seleccion_ids,  # para checkboxes
        'grupos': grupos,
        'solo_asignados': modo.solo_asignados,
    })

@login_required
@require_http_methods(["POST"])
@tens_or_admin_required
@transaction.atomic
def asignaciones_generar(request):
    """Genera asignación equitativa y aleatoria para HOY usando la selección de cuidadoras enviada."""
    hoy = timezone.localdate()

    # 1) Leer selección desde el formulario
    ids_str = request.POST.getlist('cuidadores')  # checkboxes
    try:
        selected_ids = [int(x) for x in ids_str]
    except ValueError:
        selected_ids = []

    # 2) Armar queryset de cuidadoras válidas (por grupo)
    base_cuidadoras = User.objects.filter(is_active=True, groups__name=CUIDADORA_GROUP)
    if selected_ids:
        cuidadoras_qs = base_cuidadoras.filter(id__in=selected_ids)
    else:
        # si el usuario no marcó nada, tomamos todas
        cuidadoras_qs = base_cuidadoras

    cuidadoras = list(cuidadoras_qs.order_by('first_name', 'username'))

    # 3) Guardar selección del día para persistirla
    modo, _ = DiaAsignacion.objects.get_or_create(fecha=hoy, defaults={'solo_asignados': False})
    modo.cuidadoras.set(cuidadoras_qs)  # persistimos la elección (puede ser vacío -> interpretado como "todas" en la vista)

    # 4) Residente activos y reparto
    residentes = list(Residente.objects.filter(activo=True).order_by('nombre_completo'))

    if not cuidadoras or not residentes:
        messages.error(request, "Faltan cuidadoras seleccionadas o no hay residentes activos.")
        return redirect('asignaciones_hoy')

    random.shuffle(residentes)  # aleatorio
    Asignacion.objects.filter(fecha=hoy).delete()

    bulk = []
    for i, r in enumerate(residentes):
        c = cuidadoras[i % len(cuidadoras)]  # reparto equitativo/round-robin
        bulk.append(Asignacion(fecha=hoy, cuidadora=c, residente=r))
    Asignacion.objects.bulk_create(bulk)

    messages.success(request, f"Se asignaron {len(residentes)} residentes entre {len(cuidadoras)} cuidadoras.")
    return redirect('asignaciones_hoy')

@login_required
@require_http_methods(["POST"])
@tens_or_admin_required
def asignaciones_toggle_modo(request):
    """Activa/Desactiva modo 'solo asignados' para HOY."""
    hoy = timezone.localdate()
    modo, _ = DiaAsignacion.objects.get_or_create(fecha=hoy, defaults={'solo_asignados': False})
    modo.solo_asignados = request.POST.get('solo_asignados') == '1'
    modo.save(update_fields=['solo_asignados'])
    messages.success(request, f"Modo del día: {'solo asignados' if modo.solo_asignados else 'todos los pacientes'}.")
    return redirect('asignaciones_hoy')

@login_required
@require_http_methods(["POST"])
@tens_or_admin_required
def asignaciones_limpiar(request):
    """Elimina todas las asignaciones de HOY."""
    hoy = timezone.localdate()
    deleted, _ = Asignacion.objects.filter(fecha=hoy).delete()
    messages.success(request, f"Se eliminaron {deleted} asignaciones de hoy.")
    return redirect('asignaciones_hoy')
