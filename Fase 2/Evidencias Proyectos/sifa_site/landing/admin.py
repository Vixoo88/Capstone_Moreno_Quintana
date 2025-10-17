from django.contrib import admin
from .models import Residente, Producto, Receta, OrdenMedicamento, HoraProgramada, Administracion

@admin.register(Residente)
class ResidenteAdmin(admin.ModelAdmin):
    list_display = ("nombre_completo", "rut", "sexo", "activo")
    search_fields = ("nombre_completo", "rut")
    list_filter = ("activo", "sexo")

@admin.register(Producto)
class ProductoAdmin(admin.ModelAdmin):
    list_display = ("nombre", "potencia", "forma")
    search_fields = ("nombre",)

class HoraInline(admin.TabularInline):
    model = HoraProgramada
    extra = 0

class OrdenInline(admin.StackedInline):
    model = OrdenMedicamento
    extra = 0

@admin.register(Receta)
class RecetaAdmin(admin.ModelAdmin):
    list_display = ("id", "residente", "medico", "inicio", "fin", "activa")
    list_filter = ("activa",)
    inlines = [OrdenInline]

@admin.register(OrdenMedicamento)
class OrdenAdmin(admin.ModelAdmin):
    list_display = ("id", "receta", "producto", "dosis", "activo")
    inlines = [HoraInline]

@admin.register(Administracion)
class AdministracionAdmin(admin.ModelAdmin):
    list_display = ("residente", "orden", "programada_para", "estado", "realizada_por")
    list_filter = ("estado", "programada_para")
    search_fields = ("residente__nombre_completo",)
