from collections import defaultdict
from calendar import monthrange
from datetime import datetime, time as dtime

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Q, Prefetch, F
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from django.views.decorators.http import require_POST
from django.contrib.auth import logout

from .models import (
    Administracion, HoraProgramada, OrdenMedicamento, Receta, Residente
)
from .forms import (
    AdminMarcarForm, OrdenMedicamentoForm, ProductoQuickForm,
    RecetaForm, ResidenteForm
)

# --------------------- Helpers ---------------------

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


# ---------------- Landing / Dashboard ----------------

def home_public(request):
    if request.user.is_authenticated:
        return redirect('dashboard')
    return render(request, 'landing/home.html')

@login_required
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
            messages.success(request, f'Se añadieron {sumar} al stock de {orden}.')
        else:
            messages.error(request, 'Cantidad inválida.')
    return redirect('dashboard')

@require_POST
def logout_view(request):
    logout(request)
    messages.success(request, "Sesión cerrada. ¡Hasta luego!")
    return redirect('home_public')

# ------------------- Residentes -------------------

@login_required
def residente_list(request):
    qs = Residente.objects.filter(activo=True).order_by('nombre_completo')
    return render(request, 'landing/residentes_list.html', {'residentes': qs})

@login_required
def residente_create(request):
    form = ResidenteForm(request.POST or None)
    if request.method == 'POST':
        if form.is_valid():
            r = form.save()
            messages.success(request, 'Residente creado. Ahora registra una receta.')
            return redirect('receta_create', residente_id=r.id)
        messages.error(request, 'No se pudo guardar el residente. Revisa los campos.')
    return render(request, 'landing/residente_form.html', {'form': form})

@login_required
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
    return render(request, 'landing/residente_detail.html', {'residente': res})


# ------------------- Recetas / Órdenes -------------------

@login_required
@transaction.atomic
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

            for item in horas_data:
                HoraProgramada.objects.create(
                    orden=orden, hora=item['hora'], dia_semana=item['dia']
                )

            messages.success(request, 'Receta creada correctamente.')
            return redirect('residente_detail', residente_id=res.id)

        messages.error(request, 'No se pudo guardar la receta. Revisa los errores.')

    return render(request, 'landing/receta_form.html', {
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
def receta_delete(request, receta_id):
    receta = get_object_or_404(Receta, pk=receta_id)
    res_id = receta.residente_id
    if request.method == 'POST':
        receta.delete()
        messages.success(request, 'Receta eliminada.')
        return redirect('residente_detail', residente_id=res_id)

    return render(request, 'landing/confirm_delete.html', {
        'titulo': 'Eliminar receta',
        'detalle': f'Receta #{receta.id} del residente {receta.residente}',
        'post_url': request.path,
        'volver_url': 'residente_detail',
        'volver_args': [res_id],
    })

@login_required
@transaction.atomic
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

            for item in horas_data:
                HoraProgramada.objects.create(
                    orden=orden, hora=item['hora'], dia_semana=item['dia']
                )

            messages.success(request, 'Medicamento agregado a la receta.')
            return redirect('residente_detail', residente_id=receta.residente_id)

        messages.error(request, 'No se pudo agregar el medicamento. Revisa los errores.')

    return render(request, 'landing/orden_form.html', {
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

    return render(request, 'landing/orden_form.html', {
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
def orden_delete(request, orden_id):
    orden = get_object_or_404(OrdenMedicamento, pk=orden_id)
    res_id = orden.receta.residente_id
    if request.method == 'POST':
        orden.delete()
        messages.success(request, 'Medicamento eliminado.')
        return redirect('residente_detail', residente_id=res_id)
    return render(request, 'landing/confirm_delete.html', {
        'titulo': 'Eliminar medicamento',
        'detalle': f'{orden.producto} — {orden.dosis}',
        'post_url': request.path,
        'volver_url': 'residente_detail',
        'volver_args': [res_id],
    })


# ------------------- Administración HOY -------------------

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
def admin_list_hoy(request):
    """Listado con chips por hora; permite filtrar por ?h=HH:MM."""
    _generar_eventos_hoy()
    hoy = timezone.localdate()

    eventos_qs = (
        Administracion.objects.select_related("orden__producto", "residente", "realizada_por")
        .filter(programada_para__date=hoy)
    )

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

    return render(request, 'landing/admin_hoy.html', {
        'grupos': grupos,
        'hoy': hoy,
        'horas': horas,
        'seleccion': selected,
    })

@login_required
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
    return render(request, 'landing/admin_marcar.html', {'evento': evento, 'form': form})


# ------------------- Registro mensual -------------------

@login_required
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

    return render(request, 'landing/registro_mensual.html', {
        'residente': res,
        'year': y,
        'month': m,
        'days': range(1, days_in_month + 1),
        'rows': rows
    })

@login_required
@transaction.atomic
def residente_delete(request, residente_id):
    """
    Elimina al residente y todo su historial en un orden seguro para evitar ProtectedError.
    1) Administraciones del residente
    2) Recetas del residente (borra órdenes y horas por CASCADE)
    3) Residente
    """
    residente = get_object_or_404(Residente, pk=residente_id)

    if request.method == 'POST':
        nombre = residente.nombre_completo

        # 1) Administraciones (PROTECT sobre orden/residente → hay que borrarlas primero)
        Administracion.objects.filter(residente=residente).delete()

        # 2) Recetas (tienen FK a residente con PROTECT, así que hay que borrarlas antes de borrar al residente)
        Receta.objects.filter(residente=residente).delete()

        # 3) Finalmente el residente
        residente.delete()

        messages.success(request, f'Se eliminó a "{nombre}" y todo su historial.')
        return redirect('residente_list')

    return render(request, 'landing/confirm_delete.html', {
        'titulo': 'Eliminar residente',
        'detalle': f'Se eliminará al residente "{residente.nombre_completo}" y todo su historial '
                   '(recetas, medicamentos, horas y registros de administración).',
        'post_url': request.path,
        'volver_url': 'residente_detail',
        'volver_args': [residente.id],
    })