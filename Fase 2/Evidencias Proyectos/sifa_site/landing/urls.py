from django.urls import path
from . import views
from django.contrib.auth.views import LogoutView

urlpatterns = [
    path('', views.home_public, name='home_public'),
    path('dashboard/', views.dashboard, name='dashboard'),

    path('residentes/', views.residente_list, name='residente_list'),
    path('residentes/nuevo/', views.residente_create, name='residente_create'),
    path('residentes/<int:residente_id>/', views.residente_detail, name='residente_detail'),

    path('recetas/nueva/<int:residente_id>/', views.receta_create, name='receta_create'),
    path('recetas/<int:receta_id>/eliminar/', views.receta_delete, name='receta_delete'),

    path('orden/nueva/<int:receta_id>/', views.orden_create, name='orden_create'),
    path('orden/<int:orden_id>/editar/', views.orden_edit, name='orden_edit'),
    path('orden/<int:orden_id>/eliminar/', views.orden_delete, name='orden_delete'),
    path('orden/<int:orden_id>/restock/', views.orden_restock, name='orden_restock'),

    path('administracion/', views.admin_list_hoy, name='admin_list_hoy'),
    path('administracion/quick/<int:admin_id>/', views.admin_marcar_rapido, name='admin_marcar_rapido'),
    path('administracion/grupo/', views.admin_marcar_grupo, name='admin_marcar_grupo'),
    path('administracion/marcar/<int:admin_id>/', views.admin_marcar, name='admin_marcar'),

    path('registro/<int:residente_id>/', views.registro_mensual, name='registro_mensual'),
    path('residentes/<int:residente_id>/registro-mensual/pdf/', views.registro_mensual_pdf, name='registro_mensual_pdf'),


    path("auth/logout/", LogoutView.as_view(next_page="home_public"), name="logout"),

    path('residentes/<int:residente_id>/eliminar/', views.residente_delete, name='residente_delete'),

    path('api/productos/suggest/', views.api_productos_suggest, name='api_productos_suggest'),

    path("asignaciones/", views.asignaciones_hoy, name="asignaciones_hoy"),
    path("asignaciones/generar/", views.asignaciones_generar, name="asignaciones_generar"),
    path("asignaciones/toggle/", views.asignaciones_toggle_modo, name="asignaciones_toggle_modo"),
    path("asignaciones/limpiar/", views.asignaciones_limpiar, name="asignaciones_limpiar"),


]
