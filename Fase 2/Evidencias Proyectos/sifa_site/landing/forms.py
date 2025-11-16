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




# landing/forms.py                  CRUD USUARIOS
from django import forms
from django.contrib.auth.models import User, Group

ROLE_CHOICES = [
    ("ADMIN", "Admin"),
    ("TENS", "TENS"),
    ("CUIDADORA", "Cuidadora"),
    ("DOCTOR", "Doctor"),   # << nuevo
]

def _ensure_role_group(name: str) -> Group:
    g, _ = Group.objects.get_or_create(name=name)
    return g

def assign_single_role(user: User, role_name: str):
    # Quita los roles conocidos y asigna solo el seleccionado
    known = {r for r, _ in ROLE_CHOICES}
    # Remueve
    for g in user.groups.all():
        if g.name in known:
            user.groups.remove(g)
    # Asigna
    user.groups.add(_ensure_role_group(role_name))

class AdminUserCreateForm(forms.ModelForm):
    role = forms.ChoiceField(choices=ROLE_CHOICES, label="Rol")
    password1 = forms.CharField(label="Contraseña", widget=forms.PasswordInput)
    password2 = forms.CharField(label="Confirmar contraseña", widget=forms.PasswordInput)

    class Meta:
        model = User
        fields = ["username", "first_name", "last_name", "email", "is_active"]

    def clean(self):
        cleaned = super().clean()
        p1 = cleaned.get("password1") or ""
        p2 = cleaned.get("password2") or ""
        if p1 != p2:
            self.add_error("password2", "Las contraseñas no coinciden.")
        return cleaned

    def save(self, commit=True):
        user = super().save(commit=False)
        user.set_password(self.cleaned_data["password1"])
        if commit:
            user.save()
            assign_single_role(user, self.cleaned_data["role"])
        return user

class AdminUserUpdateForm(forms.ModelForm):
    role = forms.ChoiceField(choices=ROLE_CHOICES, label="Rol")

    class Meta:
        model = User
        # 👉 Agregamos "username" para poder editarlo
        fields = ["username", "first_name", "last_name", "email", "is_active"]

    def __init__(self, *args, **kwargs):
        self.instance: User = kwargs.get("instance")
        super().__init__(*args, **kwargs)
        # Precarga el rol actual (primer grupo que coincida)
        current = next(
            (g.name for g in self.instance.groups.all()
             if g.name in dict(ROLE_CHOICES)),
            None
        )
        if current:
            self.fields["role"].initial = current

    def clean_username(self):
        username = (self.cleaned_data.get("username") or "").strip()
        if not username:
            raise forms.ValidationError("Este campo es obligatorio.")
        # Validar que no choque con otros usuarios
        qs = User.objects.filter(username=username).exclude(pk=self.instance.pk)
        if qs.exists():
            raise forms.ValidationError("Ya existe otro usuario con este nombre.")
        return username

    def save(self, commit=True):
        user = super().save(commit)
        assign_single_role(user, self.cleaned_data["role"])
        return user


class AdminUserPasswordForm(forms.Form):
    password1 = forms.CharField(label="Nueva contraseña", widget=forms.PasswordInput)
    password2 = forms.CharField(label="Confirmar contraseña", widget=forms.PasswordInput)

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("password1") != cleaned.get("password2"):
            self.add_error("password2", "Las contraseñas no coinciden.")
        return cleaned


# landing/forms.py             CRUD MEDICAMENTOS
from django import forms
from .models import Producto

class ProductoForm(forms.ModelForm):
    class Meta:
        model = Producto
        fields = ["nombre", "potencia", "forma"]
        widgets = {
            "nombre": forms.TextInput(attrs={"class": "form-control", "placeholder": "Paracetamol"}),
            "potencia": forms.TextInput(attrs={"class": "form-control", "placeholder": "500 mg"}),
            "forma": forms.TextInput(attrs={"class": "form-control", "placeholder": "Tabletas"}),
        }

    def clean(self):
        cleaned = super().clean()
        # normaliza espacios
        for k in ("nombre", "potencia", "forma"):
            if cleaned.get(k):
                cleaned[k] = " ".join(str(cleaned[k]).split())
        return cleaned
