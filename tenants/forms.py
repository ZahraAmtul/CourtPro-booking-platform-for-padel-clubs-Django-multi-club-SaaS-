from django import forms
from django.contrib.auth import authenticate, get_user_model
from django.contrib.auth.password_validation import validate_password

from bookings.forms import clean_phone_value

from .models import Club, validate_subdomain


class ClubSignupForm(forms.Form):
    club_name = forms.CharField(max_length=80, widget=forms.TextInput(attrs={"placeholder": "Smash Padel Club"}))
    subdomain = forms.CharField(max_length=40, widget=forms.TextInput(attrs={"placeholder": "smash"}))
    city = forms.CharField(max_length=60, required=False)
    owner_name = forms.CharField(label="Your name", max_length=120)
    email = forms.EmailField()
    phone = forms.CharField(label="Mobile / WhatsApp", max_length=20)
    password = forms.CharField(widget=forms.PasswordInput, min_length=8)

    def clean_subdomain(self):
        slug = self.cleaned_data["subdomain"].strip().lower()
        validate_subdomain(slug)
        if Club.objects.filter(slug=slug).exists():
            raise forms.ValidationError("That address is taken — try another.")
        return slug

    def clean_phone(self):
        return clean_phone_value(self.cleaned_data["phone"])

    def clean(self):
        data = super().clean()
        email = (data.get("email") or "").strip().lower()
        data["email"] = email
        self.existing_user = get_user_model().objects.filter(username=email).first()
        if self.existing_user and data.get("password"):
            if not authenticate(username=email, password=data["password"]):
                self.add_error("email", "This email already has an account. Enter its password to add a new club.")
        elif data.get("password"):
            try:
                validate_password(data["password"])
            except forms.ValidationError as exc:
                self.add_error("password", exc)
        return data


class ClubAdminForm(forms.ModelForm):
    class Meta:
        model = Club
        fields = ["plan", "custom_domain", "trial_ends_at", "paid_until", "is_suspended", "notes"]
        widgets = {
            "trial_ends_at": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
            "paid_until": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
            "notes": forms.Textarea(attrs={"rows": 2}),
        }
