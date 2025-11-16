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
from django.db.models import Max
from django.db.models import Count
# arriba en views.py
from .roles import (
    admin_required, cuidadora_or_admin_required, tens_or_admin_required, staff_view_required,
    is_cuidadora, is_tens, is_admin, doctor_or_admin_required, doctor_tens_or_admin_required,
    CUIDADORA_GROUP,
    TENS_GROUP

)

from django.http import HttpResponse
from django.template.loader import render_to_string
try:
    from weasyprint import HTML, CSS
    _weasy_available = True
except Exception:
    _weasy_available = False

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
    DADA y RECHAZADA consumen 1 del stock_asignado.
    - Si pasa de NO consumir → consumir (DADA/RECHAZADA) => stock - 1
    - Si pasa de consumir (DADA/RECHAZADA) → NO consumir => stock + 1
    """
    orden = evento.orden

    if not hasattr(orden, 'stock_asignado'):
        return

    def consume(estado):
        return estado in ('DADA', 'RECHAZADA')

    # Entrar a estado que consume
    if not consume(old) and consume(new):
        if (orden.stock_asignado or 0) > 0:
            orden.stock_asignado -= 1
            orden.save(update_fields=['stock_asignado'])

    # Salir de estado que consume
    elif consume(old) and not consume(new):
        orden.stock_asignado = (orden.stock_asignado or 0) + 1
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
    # Asegura que existan los eventos de hoy (idempotente)
    _generar_eventos_hoy()

    # Rango del día local (00:00:00 a 23:59:59.999999)
    hoy = timezone.localdate()
    inicio = timezone.make_aware(datetime.combine(hoy, dtime.min))
    fin    = timezone.make_aware(datetime.combine(hoy, dtime.max))

    # Query base de administraciones de hoy
    base_admins_qs = (
        Administracion.objects
        .filter(programada_para__range=(inicio, fin))
    )

    # === Si es CUIDADORA: solo sus residentes asignados hoy ===
    if is_cuidadora(request.user):
        res_ids = (
            Asignacion.objects
            .filter(fecha=hoy, cuidadora=request.user)
            .values_list('residente_id', flat=True)
        )
        admins_hoy = base_admins_qs.filter(residente_id__in=res_ids).count()

        # Para cuidadora no usaremos alertas de stock en el dashboard
        criticos = None

    # === Otros roles (admin, TENS, doctor) → ven global ===
    else:
        # Conteo robusto de administraciones de hoy (todas)
        admins_hoy = base_admins_qs.count()

        # Alertas de stock crítico (igual que antes)
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
@doctor_tens_or_admin_required
def residente_list(request):
    q = (request.GET.get("q") or "").strip()
    sexo = (request.GET.get("sexo") or "").upper()     # "M", "F", "O" o ""
    estado = (request.GET.get("estado") or "").upper() # "", "A", "I"

    # 👇 Ya no filtramos activo=True de entrada
    qs = Residente.objects.all()

    # Búsqueda por nombre o RUT
    if q:
        qs = qs.filter(
            Q(nombre_completo__icontains=q) |
            Q(rut__icontains=q)
        )

    # Filtro por sexo
    if sexo in ("M", "F", "O"):
        qs = qs.filter(sexo=sexo)

    # Filtro por estado
    if estado == "A":
        qs = qs.filter(activo=True)
    elif estado == "I":
        qs = qs.filter(activo=False)
    # si estado == "" → no se filtra por activo (muestra activos + inactivos)

    qs = qs.order_by("nombre_completo")

    return render(request, "residentes/residentes_list.html", {
        "residentes": qs,
        "q": q,
        "sexo": sexo,
        "estado": estado,      # 👈 IMPORTANTE para que el select quede marcado
        "total": qs.count(),
    })

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
@doctor_tens_or_admin_required
def residente_detail(request, residente_id):
    # Prefetch de recetas con el médico ya “pegado”
    recetas_qs = (
        Receta.objects
        .select_related('medico')
        .order_by('-creada_en')
        .prefetch_related(
            Prefetch(
                'ordenes',
                queryset=OrdenMedicamento.objects
                    .select_related('producto')
                    .prefetch_related('horas')
            )
        )
    )

    residente = get_object_or_404(
        Residente.objects.prefetch_related(
            Prefetch('recetas', queryset=recetas_qs)
        ),
        pk=residente_id
    )

    # === POST: actualizar stock de un medicamento (solo Enfermera/ADMIN) ===
    if request.method == "POST" and request.POST.get("accion") == "actualizar_stock":
        # seguridad extra: solo admin/el rol Enfermera
        if not is_admin(request.user):
            return HttpResponseForbidden("No tienes permisos para modificar el stock.")

        orden_id = request.POST.get("orden_id")
        orden = get_object_or_404(
            OrdenMedicamento,
            pk=orden_id,
            receta__residente=residente,
        )

        # Leer valores del formulario
        try:
            stock_asignado = int(request.POST.get("stock_asignado") or 0)
            stock_critico  = int(request.POST.get("stock_critico") or 0)
        except ValueError:
            messages.error(request, "Los valores de stock deben ser números.")
        else:
            if stock_asignado < 0 or stock_critico < 0:
                messages.error(request, "El stock no puede ser negativo.")
            else:
                orden.stock_asignado = stock_asignado
                orden.stock_critico  = stock_critico
                orden.save(update_fields=["stock_asignado", "stock_critico"])
                # reutilizamos la lógica de alerta
                _check_alerta_stock(orden)
                messages.success(
                    request,
                    f'Se actualizó el stock de "{orden.producto.nombre}".'
                )

        # Siempre volvemos al mismo detalle
        return redirect("residente_detail", residente_id=residente.id)

    # === GET normal: solo mostrar ficha ===
    return render(request, 'residentes/residente_detail.html', {
        'residente': residente,
    })



@login_required
@transaction.atomic
@admin_required
def residente_delete(request, residente_id):
    """
    Marca al residente como INACTIVO.
    No elimina historial ni recetas.
    """
    residente = get_object_or_404(Residente, pk=residente_id)

    if request.method == 'POST':
        nombre = residente.nombre_completo

        if residente.activo:
            residente.activo = False
            residente.save()
            messages.success(
                request,
                f'Se marcó como INACTIVO a "{nombre}". '
                'Su historial y tratamientos se mantienen registrados.'
            )
        else:
            messages.info(
                request,
                f'"{nombre}" ya se encontraba inactivo.'
            )

        return redirect('residente_list')

    # Si alguien entra por GET a esta URL, lo mandamos a la ficha
    return redirect('residente_detail', residente_id=residente.id)



# =========================================================
# Recetas / Órdenes
# =========================================================

@login_required
@transaction.atomic
@doctor_or_admin_required
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
            with transaction.atomic():
                # Crear receta y asignar número secuencial por residente
                receta = receta_form.save(commit=False)
                receta.residente = res
                receta.medico = request.user

                # Bloquea filas del residente para evitar colisiones y calcula el siguiente número
                max_num = (
                    Receta.objects
                    .select_for_update()
                    .filter(residente=res)
                    .aggregate(m=Max('numero'))['m'] or 0
                )
                receta.numero = max_num + 1
                receta.save()

                # Producto (existente o nuevo)
                producto = prod_sel or producto_form.create_if_filled()

                # Orden
                orden = orden_form.save(commit=False)
                orden.receta = receta
                orden.producto = producto
                orden.save()
                _check_alerta_stock(orden)

                # Horas
                for item in horas_data:
                    HoraProgramada.objects.create(
                        orden=orden, hora=item['hora'], dia_semana=item['dia']
                    )

            messages.success(request, f'Receta #{receta.numero} creada correctamente.')
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
@doctor_or_admin_required
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
@doctor_or_admin_required
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
@doctor_or_admin_required
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
@doctor_or_admin_required
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

    q = (request.GET.get("q") or "").strip()
    selected = request.GET.get("h")           # hora HH:MM
    cuid_param = request.GET.get("cuid")      # id de cuidadora/TENS

    user = request.user
    es_admin = is_admin(user)
    es_cuidadora = is_cuidadora(user)
    es_tens = is_tens(user)

    # === Asignaciones de hoy (base) ===
    asigns_qs = (
        Asignacion.objects
        .select_related('cuidadora')
        .filter(fecha=hoy)
    )

    # === Administraciones de hoy (base) ===
    eventos_qs = (
        Administracion.objects
        .select_related("orden__producto", "residente", "realizada_por")
        .filter(programada_para__date=hoy)
    )

    # === Restricción por rol ===
    # - ADMIN: puede ver todo y filtrar por ?cuid=
    # - NO ADMIN (cuidadora / TENS): solo lo que está asignado a SÍ MISMA
    cuid_selected = None

    if es_admin:
        # Lista de cuidadores asignados hoy (para el offcanvas)
        cuid_list = (
            asigns_qs.values(
                'cuidadora_id',
                'cuidadora__first_name',
                'cuidadora__last_name',
                'cuidadora__username',
            )
            .annotate(n=Count('id'))
            .order_by('cuidadora__first_name', 'cuidadora__username')
        )

        # Si admin pasa ?cuid=, filtramos solo a esa persona
        if cuid_param:
            try:
                cuid_id = int(cuid_param)
            except (TypeError, ValueError):
                cuid_id = None

            if cuid_id:
                res_ids = list(
                    asigns_qs
                    .filter(cuidadora_id=cuid_id)
                    .values_list('residente_id', flat=True)
                )
                eventos_qs = eventos_qs.filter(residente_id__in=res_ids)
                cuid_selected = User.objects.filter(id=cuid_id).first()
    else:
        # NO ADMIN: forzamos a ver SOLO sus residentes asignados
        asigns_qs = asigns_qs.filter(cuidadora=user)
        res_ids = list(
            asigns_qs.values_list('residente_id', flat=True)
        )
        eventos_qs = eventos_qs.filter(residente_id__in=res_ids)

        # No exponemos lista de cuidadores a no-admin
        cuid_list = []
        cuid_selected = None  # si quisieras, podrías setear user para mostrar un chip “Tus asignaciones”

    # === Filtro por nombre de residente ===
    if q:
        eventos_qs = eventos_qs.filter(residente__nombre_completo__icontains=q)

    # === Agrupar por hora HH:MM ===
    buckets, counts = defaultdict(list), defaultdict(int)
    for e in eventos_qs:
        hhmm = timezone.localtime(e.programada_para).strftime("%H:%M")
        buckets[hhmm].append(e)
        counts[hhmm] += 1

    def keyf(h): 
        return (int(h[:2]), int(h[3:5]))

    horas_sorted = sorted(counts.keys(), key=keyf)

    if selected:
        grupos = {
            selected: sorted(
                buckets.get(selected, []),
                key=lambda x: x.residente.nombre_completo
            )
        }
    else:
        grupos = {
            h: sorted(buckets[h], key=lambda x: x.residente.nombre_completo)
            for h in horas_sorted
        }

    horas = [(h, counts[h]) for h in horas_sorted]

    return render(request, 'administracion/admin_hoy.html', {
        'grupos': grupos,
        'hoy': hoy,
        'horas': horas,
        'seleccion': selected,
        'q': q,
        'cuid_list': list(cuid_list),
        'cuid_selected': cuid_selected,
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

# helpers (colócalos cerca de tus otros helpers)
from datetime import date

def _calc_edad(fn):
    if not fn:
        return None
    hoy = timezone.localdate()
    return hoy.year - fn.year - ((hoy.month, hoy.day) < (fn.month, fn.day))


from calendar import monthrange
from datetime import datetime
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.contrib.auth.decorators import login_required
from .models import Administracion, Residente
from .roles import admin_required

@login_required
@doctor_tens_or_admin_required
def registro_mensual(request, residente_id):
    """
    Una fila por (orden, hora local). En cada celda:
    ✓ (Ana), ✕ (Pedro), R (Luis) o • si está pendiente.
    Si hubiera más de un registro ese día para la misma fila, se concatena con '/'.
    """
    # --- helper local para edad ---
    def _calc_edad(fn):
        if not fn:
            return None
        hoy = timezone.localdate()
        return hoy.year - fn.year - ((hoy.month, hoy.day) < (fn.month, fn.day))

    res = get_object_or_404(Residente, pk=residente_id)

    # Año/mes con fallback seguro
    hoy_local = timezone.localdate()
    try:
        y = int(request.GET.get('year', hoy_local.year))
    except (TypeError, ValueError):
        y = hoy_local.year
    try:
        m = int(request.GET.get('month', hoy_local.month))
    except (TypeError, ValueError):
        m = hoy_local.month

    # Guardas suaves
    if y < 2000 or y > 2100:
        y = hoy_local.year
    if m < 1 or m > 12:
        m = hoy_local.month

    days_in_month = monthrange(y, m)[1]

    # Ventana de tiempo (aware)
    inicio = timezone.make_aware(datetime(y, m, 1, 0, 0))
    fin = timezone.make_aware(datetime(y, m, days_in_month, 23, 59))

    # Eventos del mes del residente
    eventos = (
        Administracion.objects
        .select_related("orden__producto", "realizada_por")
        .filter(residente=res, programada_para__range=(inicio, fin))
        .order_by("orden_id", "programada_para")
    )

    # key = (orden_id, HH:MM local) → una fila por hora específica
    rows_map = {}
    for e in eventos:
        local_dt = timezone.localtime(e.programada_para)
        hhmm_24 = local_dt.strftime("%H:%M")
        label_hora = _fmt_ampm(e.programada_para)  # ya definida en tu archivo

        prod = e.orden.producto
        med_label = f"{prod.nombre} {prod.potencia}".strip()
        orden_label = f"{med_label} · {e.orden.dosis} — {label_hora}"

        key = (e.orden_id, hhmm_24)
        if key not in rows_map:
            rows_map[key] = {"label": orden_label, "cells": [""] * days_in_month}

        sym = {"DADA": "✓", "OMITIDA": "✕", "RECHAZADA": "R", "PENDIENTE": "•"}[e.estado]
        who = _short_user(e.realizada_por) if e.estado != 'PENDIENTE' and e.realizada_por else ''  # _short_user ya definida
        mark = f"{sym}{f' ({who})' if who else ''}"

        idx = local_dt.day - 1
        rows_map[key]["cells"][idx] = (
            rows_map[key]["cells"][idx] + " / " if rows_map[key]["cells"][idx] else ""
        ) + mark

    # Orden: por nombre de medicamento y hora
    def sort_key(item):
        (orden_id, hhmm), data = item
        h, m_ = int(hhmm[:2]), int(hhmm[3:5])
        return (data["label"].split(" — ")[0].lower(), h, m_)

    rows = [v for _, v in sorted(rows_map.items(), key=sort_key)]

    # Ficha del paciente para cabecera/impresión
    paciente = {
        "nombre": res.nombre_completo,
        "rut": res.rut,
        "fecha_nacimiento": res.fecha_nacimiento,
        "edad": _calc_edad(res.fecha_nacimiento),
        "sexo": res.get_sexo_display() if hasattr(res, "get_sexo_display") else res.sexo,
        "alergias": (res.alergias or "").strip(),
        "activo": res.activo,
    }

    return render(request, 'residentes/registro_mensual.html', {
        'residente': res,
        'year': y,
        'month': m,
        'days': range(1, days_in_month + 1),
        'rows': rows,
        'paciente': paciente,   # <-- ficha para el template
    })



def _build_registro_mensual_ctx(res, y, m):
    """
    Copia 99% de la lógica de 'registro_mensual' para construir days/rows.
    Así no duplicamos reglas sueltas, y la tabla del PDF queda idéntica.
    """
    days_in_month = monthrange(y, m)[1]
    inicio = timezone.make_aware(datetime(y, m, 1, 0, 0))
    fin = timezone.make_aware(datetime(y, m, days_in_month, 23, 59))

    eventos = (
        Administracion.objects
        .select_related("orden__producto", "realizada_por")
        .filter(residente=res, programada_para__range=(inicio, fin))
        .order_by("orden_id", "programada_para")
    )

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

    def sort_key(item):
        (orden_id, hhmm), data = item
        h, mm = int(hhmm[:2]), int(hhmm[3:5])
        return (data["label"].split(" — ")[0].lower(), h, mm)

    rows = [v for _, v in sorted(rows_map.items(), key=sort_key)]

    return {
        "days": range(1, days_in_month + 1),
        "rows": rows,
        "days_in_month": days_in_month,
    }


# --- ADD: view PDF ---
@login_required
@admin_required
def registro_mensual_pdf(request, residente_id):
    """
    Genera un PDF A4 apaisado del registro mensual del residente.
    """
    if not _weasy_available:
        return HttpResponse("WeasyPrint no está instalado en el servidor.", status=500)

    res = get_object_or_404(Residente, pk=residente_id)

    hoy_local = timezone.localdate()
    try:
        y = int(request.GET.get('year', hoy_local.year))
        m = int(request.GET.get('month', hoy_local.month))
    except (TypeError, ValueError):
        y, m = hoy_local.year, hoy_local.month

    data = _build_registro_mensual_ctx(res, y, m)

    ctx = {
        "residente": res,
        "year": y,
        "month": m,
        "days": data["days"],
        "rows": data["rows"],
        "generated_at": timezone.localdate(),
    }

    html_string = render_to_string("residentes/registro_mensual_pdf.html", ctx, request=request)

    css = CSS(string="""
      @page { size: A4 landscape; margin: 10mm; }
      body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, 'Noto Sans', sans-serif; }
      h3 { margin: 0 0 6px 0; }
      .meta { color: #666; font-size: 11px; margin-bottom: 8px; }
      table { width: 100%; border-collapse: collapse; font-size: 11px; table-layout: fixed; }
      thead th { background: #eee; border: 1px solid #ccc; padding: 6px; }
      td { border: 1px solid #ddd; padding: 4px 6px; text-align: center; word-wrap: break-word; }
      td.label, th.label { text-align: left; font-weight: 600; width: 320px; }
      thead { display: table-header-group; }
      tr { page-break-inside: avoid; }
      .legend { color:#555; font-size: 10px; margin-top: 6px; }
    """)

    base_url = request.build_absolute_uri('/')  # por si referencias estáticos o imágenes
    pdf = HTML(string=html_string, base_url=base_url).write_pdf(stylesheets=[css])

    filename = f"registro_{res.id}_{y}-{m:02d}.pdf"
    resp = HttpResponse(pdf, content_type="application/pdf")
    resp["Content-Disposition"] = f'attachment; filename="{filename}"'
    return resp



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

from datetime import datetime, time as dtime

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.db import transaction
from django.shortcuts import render, redirect
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from .roles import (
    tens_or_admin_required,
    CUIDADORA_GROUP,
    TENS_GROUP,
)
from .models import DiaAsignacion, Asignacion, Administracion, Residente




# landing/views.py            CRUD USUARIOS
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User, Group
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.db.models import Q

from .roles import admin_required  # ya lo tienes
from .forms import (
    AdminUserCreateForm, AdminUserUpdateForm, AdminUserPasswordForm,
    assign_single_role, _ensure_role_group, ROLE_CHOICES
)

# Orden deseado de roles
ROLE_ORDER = {
    "ADMIN": 0,      # Enfermera/o
    "TENS": 2,
    "CUIDADORA": 3,
    "DOCTOR": 1,
}

# Lista de usuarios (búsqueda + orden por rol)
@login_required
@admin_required
def user_list(request):
    q   = (request.GET.get("q") or "").strip()
    rol = (request.GET.get("rol") or "").strip()   # código de rol (nombre del grupo)

    # Base queryset
    users = User.objects.all()

    # Búsqueda de texto
    if q:
        users = users.filter(
            Q(username__icontains=q) |
            Q(first_name__icontains=q) |
            Q(last_name__icontains=q) |
            Q(email__icontains=q)
        )

    # Filtro por rol (grupo)
    if rol:
        users = users.filter(groups__name=rol)

    # Para no hacer mil consultas por usuario
    users = users.prefetch_related("groups").distinct()

    # Conjunto de códigos válidos de rol (ADMIN, TENS, CUIDADORA, DOCTOR)
    known_codes = {code for code, _ in ROLE_CHOICES}

    items = []
    for u in users:
        # buscamos el primer grupo cuyo nombre esté en ROLE_CHOICES
        code = next((g.name for g in u.groups.all() if g.name in known_codes), None)
        # guardamos (usuario, código_de_rol) -> el template se encarga de mostrar texto y color
        items.append((u, code))

    # Ordenar por rol (según ROLE_ORDER) y luego por nombre
    items.sort(
        key=lambda t: (
            ROLE_ORDER.get(t[1], 99),                     # primero por rol
            (t[0].first_name or "").lower(),             # luego por nombre
            (t[0].last_name or "").lower(),              # luego apellido
            t[0].username.lower(),                       # luego username
        )
    )

    return render(
        request,
        "users/user_list.html",
        {
            "items": items,
            "q": q,
            "rol": rol,                 # para marcar el option seleccionado
            "role_choices": ROLE_CHOICES,
        },
    )


# Crear usuario
@login_required
@admin_required
def user_create(request):
    if request.method == "POST":
        form = AdminUserCreateForm(request.POST)
        if form.is_valid():
            user = form.save()
            messages.success(request, f"Usuario '{user.username}' creado.")
            return redirect("user_list")
    else:
        form = AdminUserCreateForm(initial={"is_active": True})
    return render(request, "users/user_form.html", {"form": form, "title": "Crear usuario"})

# Editar usuario (perfil + rol)
@login_required
@admin_required
def user_edit(request, user_id):
    u = get_object_or_404(User, pk=user_id)
    if request.method == "POST":
        form = AdminUserUpdateForm(request.POST, instance=u)
        if form.is_valid():
            form.save()
            messages.success(request, f"Usuario '{u.username}' actualizado.")
            return redirect("user_list")
    else:
        form = AdminUserUpdateForm(instance=u)
    return render(request, "users/user_form.html", {"form": form, "title": f"Editar: {u.username}", "user_obj": u})

# Cambiar contraseña
@login_required
@admin_required
def user_password(request, user_id):
    u = get_object_or_404(User, pk=user_id)
    if request.method == "POST":
        form = AdminUserPasswordForm(request.POST)
        if form.is_valid():
            u.set_password(form.cleaned_data["password1"])
            u.save(update_fields=["password"])
            messages.success(request, f"Contraseña actualizada para '{u.username}'.")
            return redirect("user_list")
    else:
        form = AdminUserPasswordForm()
    return render(request, "users/user_password.html", {"form": form, "title": f"Contraseña: {u.username}", "user_obj": u})

# Eliminar usuario (con protección básica)
@login_required
@admin_required
def user_delete(request, user_id):
    u = get_object_or_404(User, pk=user_id)
    if request.user == u:
        messages.error(request, "No puedes eliminar tu propio usuario.")
        return redirect("user_list")
    if request.method == "POST":
        username = u.username
        u.delete()
        messages.success(request, f"Usuario '{username}' eliminado.")
        return redirect("user_list")
    return render(request, "users/user_confirm_delete.html", {"user_obj": u})




# landing/views.py            CRUD MEDICAMENTOS
from django.core.paginator import Paginator
from .forms import ProductoForm
from .roles import admin_required
from .models import Producto

@login_required
@admin_required
def medicamentos_list(request):
    q = (request.GET.get("q") or "").strip()
    qs = Producto.objects.all().order_by("nombre", "potencia")
    if q:
        qs = qs.filter(
            Q(nombre__icontains=q) | Q(potencia__icontains=q) | Q(forma__icontains=q)
        )
    paginator = Paginator(qs, 20)
    page = request.GET.get("page")
    page_obj = paginator.get_page(page)
    return render(request, "medicamentos/medicamentos_list.html", {
        "page_obj": page_obj,
        "q": q,
    })

@login_required
@admin_required
def medicamento_create(request):
    if request.method == "POST":
        form = ProductoForm(request.POST)
        if form.is_valid():
            p = form.save()
            messages.success(request, f"Medicamento '{p.nombre} {p.potencia or ''}'.")
            return redirect("medicamentos_list")
    else:
        form = ProductoForm()
    return render(request, "medicamentos/medicamento_form.html", {
        "form": form,
        "title": "Nuevo medicamento",
    })

@login_required
@admin_required
def medicamento_edit(request, producto_id):
    p = get_object_or_404(Producto, pk=producto_id)
    if request.method == "POST":
        form = ProductoForm(request.POST, instance=p)
        if form.is_valid():
            form.save()
            messages.success(request, f"Medicamento '{p.nombre}' actualizado.")
            return redirect("medicamentos_list")
    else:
        form = ProductoForm(instance=p)
    return render(request, "medicamentos/medicamento_form.html", {
        "form": form,
        "title": f"Editar medicamento",
        "obj": p,
    })

@login_required
@admin_required
def medicamento_delete(request, producto_id):
    p = get_object_or_404(Producto, pk=producto_id)
    if request.method == "POST":
        nombre = f"{p.nombre} {p.potencia or ''}".strip()
        p.delete()
        messages.success(request, f"Medicamento '{nombre}' eliminado.")
        return redirect("medicamentos_list")
    return render(request, "medicamentos/medicamento_confirm_delete.html", {
        "obj": p
    })

from datetime import datetime, time as dtime

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.db import transaction
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from .roles import tens_or_admin_required, CUIDADORA_GROUP, TENS_GROUP
from .models import DiaAsignacion, Asignacion, Administracion, Residente
from .notifications import send_telegram_message  # importante


@login_required
@tens_or_admin_required
def asignaciones_hoy(request):
    hoy = timezone.localdate()

    # CUIDADORAS + TENS ACTIVOS en la lista
    base_cuidadoras = (
        User.objects
        .filter(
            is_active=True,
            groups__name__in=[CUIDADORA_GROUP, TENS_GROUP],
        )
        .distinct()
        .order_by('first_name', 'username')
    )

    # Modo/selección del día
    modo, _ = DiaAsignacion.objects.get_or_create(
        fecha=hoy,
        defaults={'solo_asignados': False},
    )
    seleccion_ids = set(modo.cuidadoras.values_list('id', flat=True))

    # Si NO hay selección guardada → pre-marcamos TODAS (cuidadoras + TENS) ACTIVAS
    if not seleccion_ids:
        seleccion_ids = set(
            base_cuidadoras.values_list('id', flat=True)
        )

    # Asignaciones de hoy (solo cuidadoras/TENS activos y residentes activos)
    asignaciones = (
        Asignacion.objects
        .select_related('cuidadora', 'residente')
        .filter(
            fecha=hoy,
            cuidadora__is_active=True,
            residente__activo=True,
        )
        .order_by('cuidadora__username', 'residente__nombre_completo')
    )

    grupos = {}
    for a in asignaciones:
        grupos.setdefault(a.cuidadora, []).append(a.residente)

    return render(request, 'asignaciones/asignaciones_hoy.html', {
        'fecha': hoy,
        'cuidadoras': base_cuidadoras,
        'seleccion_ids': seleccion_ids,
        'grupos': grupos,
        'solo_asignados': modo.solo_asignados,
    })


@login_required
@require_http_methods(["POST"])
@tens_or_admin_required
@transaction.atomic
def asignaciones_generar(request):
    """
    Genera asignación SOLO para residentes que tienen administraciones HOY
    (pendientes si hay, o cualquiera hoy). Al final envía un resumen por Telegram.
    """
    hoy = timezone.localdate()

    # --- límites del día en tz local ---
    inicio_dia = timezone.make_aware(datetime.combine(hoy, dtime.min))
    fin_dia    = timezone.make_aware(datetime.combine(hoy, dtime.max))
    ahora      = timezone.now()

    SOLO_DESDE_AHORA = True

    # 1) Cuidadores seleccionados (CUIDADORAS + TENS ACTIVOS)
    ids_str = request.POST.getlist('cuidadores')
    try:
        selected_ids = [int(x) for x in ids_str]
    except (TypeError, ValueError):
        selected_ids = []

    base_cuidadoras = (
        User.objects
        .filter(
            is_active=True,
            groups__name__in=[CUIDADORA_GROUP, TENS_GROUP],
        )
        .distinct()
    )

    # Si hay seleccionados → solo esos; si no, TODOS (cuidadoras + TENS) ACTIVOS
    cuidadoras_qs = (
        base_cuidadoras.filter(id__in=selected_ids)
        if selected_ids
        else base_cuidadoras
    )
    cuidadoras = list(cuidadoras_qs.order_by('first_name', 'username'))

    modo, _ = DiaAsignacion.objects.get_or_create(
        fecha=hoy,
        defaults={'solo_asignados': False},
    )
    modo.cuidadoras.set(cuidadoras_qs)

    if not cuidadoras:
        messages.error(request, "No hay cuidadores seleccionados/activos.")
        return redirect('asignaciones_hoy')

    # 2) Residentes con administraciones HOY
    filtro_tiempo = {'programada_para__range': (inicio_dia, fin_dia)}
    if SOLO_DESDE_AHORA:
        filtro_tiempo = {'programada_para__range': (ahora, fin_dia)}

    # Preferir PENDIENTES
    res_ids_pend = list(
        Administracion.objects
        .filter(estado=Administracion.Estado.PENDIENTE, **filtro_tiempo)
        .values_list('residente_id', flat=True)
        .distinct()
    )

    if res_ids_pend:
        effective_res_ids = res_ids_pend
    else:
        effective_res_ids = list(
            Administracion.objects
            .filter(**filtro_tiempo)
            .values_list('residente_id', flat=True)
            .distinct()
        )

    if not effective_res_ids:
        Asignacion.objects.filter(fecha=hoy).delete()
        messages.warning(
            request,
            "Hoy no hay residentes con administraciones (vigentes) para asignar."
        )
        return redirect('asignaciones_hoy')

    residentes = list(
        Residente.objects
        .filter(id__in=effective_res_ids, activo=True)
        .order_by('nombre_completo')
    )

    if not residentes:
        Asignacion.objects.filter(fecha=hoy).delete()
        messages.warning(
            request,
            "No hay residentes activos con administraciones hoy."
        )
        return redirect('asignaciones_hoy')

    # 3) Reparto equitativo
    import random
    random.shuffle(residentes)
    Asignacion.objects.filter(fecha=hoy).delete()

    bulk = []
    for i, r in enumerate(residentes):
        c = cuidadoras[i % len(cuidadoras)]
        bulk.append(Asignacion(fecha=hoy, cuidadora=c, residente=r))
    Asignacion.objects.bulk_create(bulk)

    # 4) Mensaje interno en Django
    messages.success(
        request,
        f"Asignados {len(residentes)} residentes entre {len(cuidadoras)} cuidador(es)."
    )

    # 5) Resumen por Telegram
    resumen_por_cuidadora = {}
    for i, r in enumerate(residentes):
        c = cuidadoras[i % len(cuidadoras)]
        resumen_por_cuidadora.setdefault(c, []).append(r)

    lineas = [
        f"📋 Asignación de residentes — {hoy.strftime('%d/%m/%Y')}",
        "",
    ]
    for c, lista_res in resumen_por_cuidadora.items():
        nombre_c = c.get_full_name() or c.username
        lineas.append(f"👤 {nombre_c} → {len(lista_res)} residente(s):")
        for r in lista_res:
            lineas.append(f"   • {r.nombre_completo}")
        lineas.append("")

    texto_telegram = "\n".join(lineas)

    ok, detail = send_telegram_message(texto_telegram, return_detail=True)
    if not ok:
        messages.error(
            request,
            f"Asignación creada, pero no se pudo enviar el resumen a Telegram: {detail}"
        )

    return redirect('asignaciones_hoy')


@login_required
@require_http_methods(["POST"])
@tens_or_admin_required
def asignaciones_toggle_modo(request):
    """Activa/Desactiva modo 'solo asignados' para HOY."""
    hoy = timezone.localdate()
    modo, _ = DiaAsignacion.objects.get_or_create(
        fecha=hoy,
        defaults={'solo_asignados': False},
    )
    modo.solo_asignados = request.POST.get('solo_asignados') == '1'
    modo.save(update_fields=['solo_asignados'])
    messages.success(
        request,
        f"Modo del día: {'solo asignados' if modo.solo_asignados else 'todos los pacientes'}."
    )
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


# === AVISO RÁPIDO DE MEDICAMENTOS LISTOS ===
@login_required
@require_http_methods(["POST"])
@tens_or_admin_required
def asignaciones_avisar_meds(request):
    hoy = timezone.localdate()
    texto = (request.POST.get("mensaje") or "").strip()
    if not texto:
        texto = (
            f"💊 Medicamentos de hoy ({hoy.strftime('%d/%m/%Y')}) están listos para "
            "retiro en Enfermería. Por favor pasar a buscar y administrar. Gracias."
        )

    ok, detail = send_telegram_message(texto, return_detail=True)
    if ok:
        messages.success(request, "Aviso enviado a Telegram.")
    else:
        messages.error(request, f"No se pudo enviar el aviso a Telegram: {detail}")
    return redirect('asignaciones_hoy')








from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.shortcuts import render, redirect
from django.contrib.auth.models import User
from django import forms
from django.contrib.auth.forms import PasswordChangeForm
from django.contrib.auth import update_session_auth_hash


class MiPerfilForm(forms.ModelForm):
    class Meta:
        model = User
        # Datos que el propio usuario puede cambiar
        fields = ["username", "first_name", "last_name", "email"]
        widgets = {
            "username": forms.TextInput(attrs={
                "class": "form-control",
                "placeholder": "usuario.ejemplo"
            }),
            "first_name": forms.TextInput(attrs={
                "class": "form-control",
                "placeholder": "Nombre"
            }),
            "last_name": forms.TextInput(attrs={
                "class": "form-control",
                "placeholder": "Apellidos"
            }),
            "email": forms.EmailInput(attrs={
                "class": "form-control",
                "placeholder": "correo@ejemplo.cl"
            }),
        }


@login_required
def mi_perfil(request):
    user = request.user

    if request.method == "POST":
        # Usamos prefix para poder tener dos forms en la misma página
        profile_form = MiPerfilForm(request.POST, instance=user, prefix="profile")
        password_form = PasswordChangeForm(user, request.POST, prefix="pwd")

        # Guardar datos de cuenta
        if "btn_profile" in request.POST:
            if profile_form.is_valid():
                profile_form.save()
                messages.success(request, "Tus datos de cuenta se actualizaron correctamente.")
                return redirect("mi_perfil")

        # Cambiar contraseña
        elif "btn_password" in request.POST:
            if password_form.is_valid():
                user = password_form.save()
                # Para que no lo desloguee al cambiar la contraseña
                update_session_auth_hash(request, user)
                messages.success(request, "Tu contraseña se actualizó correctamente.")
                return redirect("mi_perfil")

    else:
        profile_form = MiPerfilForm(instance=user, prefix="profile")
        password_form = PasswordChangeForm(user, prefix="pwd")

    return render(request, "users/mi_perfil.html", {
        "title": "Mi perfil",
        "profile_form": profile_form,
        "password_form": password_form,
    })
