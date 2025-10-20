from django.conf import settings
from django.db import models
from django.core.validators import MinValueValidator


# --------- Residentes ----------
class Residente(models.Model):
    class Sexo(models.TextChoices):
        M = "M", "Masculino"
        F = "F", "Femenino"
        O = "O", "Otro/ND"

    nombre_completo = models.CharField(max_length=160)
    rut = models.CharField(max_length=20, unique=True)
    fecha_nacimiento = models.DateField(null=True, blank=True)
    sexo = models.CharField(max_length=1, choices=Sexo.choices, default=Sexo.O)
    alergias = models.TextField(blank=True)
    activo = models.BooleanField(default=True)

    def __str__(self):
        return f"{self.nombre_completo} ({self.rut})"


# --------- Catálogo de medicamentos ----------
class Producto(models.Model):
    nombre = models.CharField(max_length=160)
    potencia = models.CharField(max_length=60, blank=True)  # ej: 500 mg
    forma = models.CharField(max_length=40, blank=True)     # tableta, jarabe, etc.

    def __str__(self):
        return f"{self.nombre} {self.potencia}".strip()


# --------- Receta y programación ----------
class Receta(models.Model):
    residente = models.ForeignKey(Residente, on_delete=models.CASCADE, related_name="recetas")
    medico = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    inicio = models.DateField()
    fin = models.DateField(null=True, blank=True)
    observaciones = models.TextField(blank=True)
    activa = models.BooleanField(default=True)
    creada_en = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Receta #{self.id} · {self.residente}"


class OrdenMedicamento(models.Model):
    receta = models.ForeignKey(Receta, on_delete=models.CASCADE, related_name="ordenes")
    producto = models.ForeignKey(Producto, on_delete=models.PROTECT)
    dosis = models.CharField(max_length=60)  # ej: "1 tableta", "5 ml"
    via = models.CharField(max_length=40, blank=True)
    indicaciones = models.TextField(blank=True)
    activo = models.BooleanField(default=True)

    stock_asignado = models.PositiveIntegerField(default=0)  # stock actual
    stock_critico = models.PositiveIntegerField(default=0)

    alerta_enviada = models.BooleanField(default=False)

    def __str__(self):
        return f"{self.producto} · {self.dosis}"


class HoraProgramada(models.Model):
    """Horas fijas por día (puedes crear varias)."""
    class Dia(models.IntegerChoices):
        LUN = 0, "Lun"
        MAR = 1, "Mar"
        MIE = 2, "Mié"
        JUE = 3, "Jue"
        VIE = 4, "Vie"
        SAB = 5, "Sáb"
        DOM = 6, "Dom"

    orden = models.ForeignKey(OrdenMedicamento, on_delete=models.CASCADE, related_name="horas")
    hora = models.TimeField()
    dia_semana = models.IntegerField(choices=Dia.choices, null=True, blank=True)  # null = todos los días

    class Meta:
        ordering = ["hora"]

    def __str__(self):
        return f"{self.hora.strftime('%H:%M')}"


# --------- Administración (registro tomó/no tomó) ----------
class Administracion(models.Model):
    class Estado(models.TextChoices):
        PENDIENTE = "PENDIENTE", "Pendiente"
        DADA = "DADA", "Administrada"
        OMITIDA = "OMITIDA", "Omitida"
        RECHAZADA = "RECHAZADA", "Rechazada"

    orden = models.ForeignKey(OrdenMedicamento, on_delete=models.PROTECT, related_name="administraciones")
    residente = models.ForeignKey(Residente, on_delete=models.CASCADE)
    programada_para = models.DateTimeField()                 # fecha/hora prevista
    registrada_en = models.DateTimeField(auto_now_add=True)
    estado = models.CharField(max_length=12, choices=Estado.choices, default=Estado.PENDIENTE)
    cantidad_administrada = models.DecimalField(max_digits=10, decimal_places=3, null=True, blank=True,
                                                validators=[MinValueValidator(0)])
    observacion = models.TextField(blank=True)
    realizada_por = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
                                      null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=["programada_para", "residente"])]

    def __str__(self):
        return f"{self.residente} · {self.orden} · {self.programada_para:%Y-%m-%d %H:%M}"
