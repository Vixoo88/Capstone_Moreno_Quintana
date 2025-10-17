from django import forms
from .models import Residente, Receta, OrdenMedicamento, Administracion, Producto

class ResidenteForm(forms.ModelForm):
    class Meta:
        model = Residente
        fields = ['nombre_completo', 'rut', 'fecha_nacimiento', 'sexo', 'alergias', 'activo']
        widgets = {
            'nombre_completo': forms.TextInput(attrs={'class': 'form-control'}),
            'rut': forms.TextInput(attrs={'class': 'form-control'}),
            'fecha_nacimiento': forms.DateInput(attrs={'type': 'date', 'class': 'form-control'}),
            'sexo': forms.Select(attrs={'class': 'form-select'}),
            'alergias': forms.Textarea(attrs={'rows': 2, 'class': 'form-control'}),
            'activo': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
        }

class RecetaForm(forms.ModelForm):
    class Meta:
        model = Receta
        fields = ['inicio', 'fin', 'observaciones', 'activa']
        widgets = {
            'inicio': forms.DateInput(attrs={'type': 'date', 'class': 'form-control'}),
            'fin': forms.DateInput(attrs={'type': 'date', 'class': 'form-control'}),
            'observaciones': forms.Textarea(attrs={'rows': 2, 'class': 'form-control'}),
            'activa': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
        }

class OrdenMedicamentoForm(forms.ModelForm):
    class Meta:
        model = OrdenMedicamento
        fields = ['producto', 'dosis', 'via', 'indicaciones', 'activo', 'stock_asignado', 'stock_critico']
        widgets = {
            'producto': forms.Select(attrs={'class': 'form-select', 'id': 'id_producto'}),
            'dosis': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'p.ej. 1 tableta'}),
            'via': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'oral / IM / SC'}),
            'indicaciones': forms.Textarea(attrs={'rows': 2, 'class': 'form-control'}),
            'activo': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'stock_asignado': forms.NumberInput(attrs={'class': 'form-control', 'min': '0'}),
            'stock_critico': forms.NumberInput(attrs={'class': 'form-control', 'min': '0'}),
        }
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['producto'].required = False

class ProductoQuickForm(forms.Form):
    nombre = forms.CharField(required=False, widget=forms.TextInput(attrs={
        'class':'form-control','placeholder':'Paracetamol'
    }))
    potencia = forms.CharField(required=False, widget=forms.TextInput(attrs={
        'class':'form-control','placeholder':'500 mg'
    }))
    forma = forms.CharField(required=False, widget=forms.TextInput(attrs={
        'class':'form-control','placeholder':'Tableta / Jarabe'
    }))
    def create_if_filled(self):
        cd = self.cleaned_data
        if cd.get('nombre'):
            return Producto.objects.create(
                nombre=cd['nombre'].strip(),
                potencia=cd.get('potencia','').strip(),
                forma=cd.get('forma','').strip()
            )
        return None

class AdminMarcarForm(forms.ModelForm):
    class Meta:
        model = Administracion
        fields = ['estado','cantidad_administrada','observacion'] if hasattr(Administracion, 'cantidad_administrada') else ['estado']
        widgets = {
            'estado': forms.Select(attrs={'class': 'form-select'}),
        }
