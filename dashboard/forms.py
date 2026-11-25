from decimal import Decimal

from django import forms
from django.forms import formset_factory

from billing.models import CreditPackage, Payment
from bookings.models import Extra, PromoCode
from core.models import SiteSettings
from courts.models import Court

TIME = forms.TimeInput(attrs={"type": "time"}, format="%H:%M")
DATE = forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")


class SettingsForm(forms.ModelForm):
    class Meta:
        model = SiteSettings
        exclude = ["club"]
        widgets = {
            "open_time": TIME, "close_time": TIME, "peak_start": TIME, "peak_end": TIME,
            "invoice_footer": forms.Textarea(attrs={"rows": 2}),
            "payment_instructions": forms.Textarea(attrs={"rows": 2}),
        }

    sections = [
        ("Club profile", ["club_name", "tagline", "phone", "whatsapp", "email", "address", "map_url",
                          "instagram_url"]),
        ("Opening hours & slots", ["open_time", "close_time", "slot_minutes", "allowed_durations"]),
        ("Peak pricing", ["peak_start", "peak_end", "weekend_all_day_peak"]),
        ("Booking rules", ["booking_window_days", "min_notice_minutes", "cancellation_hours", "auto_confirm"]),
        ("Invoices & tax", ["currency_symbol", "tax_label", "tax_percent", "invoice_prefix", "invoice_due_days",
                            "payment_instructions", "invoice_footer"]),
        ("Online payments", ["prepayment_mode", "deposit_percent", "payment_hold_minutes", "payment_gateway",
                             "jazzcash_merchant_id", "jazzcash_password", "jazzcash_integrity_salt",
                             "jazzcash_sandbox"]),
        ("WhatsApp confirmations & reminders", ["whatsapp_enabled", "whatsapp_phone_number_id",
                                                "whatsapp_access_token", "whatsapp_template_language",
                                                "whatsapp_confirmation_template", "whatsapp_reminder_template",
                                                "remind_24h", "remind_2h"]),
    ]
    SECRET_FIELDS = ["jazzcash_password", "jazzcash_integrity_salt", "whatsapp_access_token"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in self.SECRET_FIELDS:
            saved = bool(getattr(self.instance, name, ""))
            self.fields[name].widget = forms.PasswordInput(
                render_value=False,
                attrs={"placeholder": "Saved — leave blank to keep" if saved else "", "autocomplete": "new-password"},
            )
            self.fields[name].required = False

    def clean(self):
        data = super().clean()
        for name in self.SECRET_FIELDS:  # blank secret field = keep the stored value
            if not data.get(name):
                data[name] = getattr(self.instance, name, "")
        return data

    def grouped(self):
        return [(title, [self[name] for name in names]) for title, names in self.sections]


class CourtForm(forms.ModelForm):
    class Meta:
        model = Court
        fields = ["name", "court_type", "description", "features", "image", "price_off_peak", "price_peak",
                  "is_active", "sort_order"]
        widgets = {"description": forms.Textarea(attrs={"rows": 3})}


class ExtraForm(forms.ModelForm):
    class Meta:
        model = Extra
        fields = ["name", "description", "price", "max_quantity", "is_active", "sort_order"]


class PromoForm(forms.ModelForm):
    class Meta:
        model = PromoCode
        fields = ["code", "description", "discount_type", "value", "min_amount", "valid_from", "valid_until",
                  "max_uses", "is_active"]
        widgets = {"valid_from": DATE, "valid_until": DATE}

    def clean(self):
        data = super().clean()
        if data.get("discount_type") == PromoCode.DiscountType.PERCENT and (data.get("value") or 0) > 100:
            self.add_error("value", "A percentage can't be more than 100.")
        return data


class PaymentForm(forms.Form):
    amount = forms.DecimalField(min_value=Decimal("0.01"), decimal_places=2)
    method = forms.ChoiceField(choices=Payment.Method.choices)
    reference = forms.CharField(required=False, max_length=80, label="Transaction ID / receipt")
    note = forms.CharField(required=False, max_length=200)


class InvoiceForm(forms.Form):
    bill_to_name = forms.CharField(label="Customer name", max_length=120)
    bill_to_phone = forms.CharField(label="Phone", max_length=20, required=False)
    bill_to_email = forms.EmailField(label="Email", required=False)
    due_date = forms.DateField(widget=DATE, required=False)
    discount = forms.DecimalField(min_value=0, decimal_places=2, required=False, initial=0)
    notes = forms.CharField(widget=forms.Textarea(attrs={"rows": 2}), required=False)


class InvoiceItemForm(forms.Form):
    description = forms.CharField(max_length=200)
    quantity = forms.DecimalField(min_value=Decimal("0.01"), decimal_places=2, initial=1)
    unit_price = forms.DecimalField(min_value=0, decimal_places=2)


InvoiceItemFormSet = formset_factory(InvoiceItemForm, extra=3, min_num=1, validate_min=True)


class TeamMemberForm(forms.Form):
    first_name = forms.CharField(max_length=60)
    email = forms.EmailField()
    role = forms.ChoiceField(choices=[("staff", "Staff (reception)"), ("owner", "Owner")], initial="staff")
    password = forms.CharField(
        widget=forms.PasswordInput, min_length=8, required=False,
        help_text="Needed for a new login. Leave blank if this person already has an account.",
    )

    def __init__(self, *args, club=None, **kwargs):
        self.club = club
        super().__init__(*args, **kwargs)

    def clean(self):
        from django.contrib.auth import get_user_model
        from django.contrib.auth.password_validation import validate_password

        from tenants.models import Membership

        data = super().clean()
        email = (data.get("email") or "").strip().lower()
        data["email"] = email
        self.user = get_user_model().objects.filter(username=email).first()
        if self.user and Membership.objects.filter(user=self.user, club=self.club).exists():
            raise forms.ValidationError("This person is already on your team.")
        if not self.user:
            if not data.get("password"):
                self.add_error("password", "Set a password for the new login.")
            else:
                validate_password(data["password"])
        return data

    def save(self):
        from django.contrib.auth import get_user_model

        from tenants.models import Membership

        data = self.cleaned_data
        if not self.user:
            self.user = get_user_model().objects.create_user(
                username=data["email"], email=data["email"], password=data["password"], first_name=data["first_name"]
            )
        return Membership.objects.create(user=self.user, club=self.club, role=data["role"])


PAID_WITH = [(m, label) for m, label in Payment.Method.choices if m not in ("wallet", "online")]


class SellCreditForm(forms.Form):
    name = forms.CharField(label="Customer name", max_length=120)
    phone = forms.CharField(label="Mobile number", max_length=20)
    package = forms.ModelChoiceField(queryset=CreditPackage.objects.none(), required=False,
                                     empty_label="Custom amount")
    price = forms.DecimalField(label="Customer pays", min_value=1, decimal_places=2, required=False)
    credit = forms.DecimalField(label="Credit to add", min_value=1, decimal_places=2, required=False,
                                help_text="Leave blank to add the same as paid")
    method = forms.ChoiceField(label="Paid by", choices=PAID_WITH)
    reference = forms.CharField(label="Receipt / transaction ID", max_length=80, required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["package"].queryset = CreditPackage.objects.filter(is_active=True)
        self.fields["package"].label_from_instance = (
            lambda p: f"{p.name} — pay {p.price:,.0f}, get {p.credit:,.0f}")

    def clean_phone(self):
        from bookings.forms import clean_phone_value

        return clean_phone_value(self.cleaned_data["phone"])

    def clean(self):
        data = super().clean()
        if not data.get("package") and not data.get("price"):
            self.add_error("price", "Choose a package or enter an amount.")
        return data


class AdjustCreditForm(forms.Form):
    amount = forms.DecimalField(decimal_places=2, help_text="Positive adds credit, negative removes it")
    note = forms.CharField(label="Reason", max_length=200)


class DayCloseForm(forms.Form):
    counted_cash = forms.DecimalField(label="Cash counted in the drawer", min_value=0, decimal_places=2)
    note = forms.CharField(max_length=300, required=False, label="Note (optional)")


class CreditPackageForm(forms.ModelForm):
    class Meta:
        model = CreditPackage
        fields = ["name", "price", "credit", "description", "is_active", "sort_order"]

    def clean(self):
        data = super().clean()
        if data.get("price") and data.get("credit") and data["credit"] < data["price"]:
            self.add_error("credit", "Credit should be at least what the customer pays.")
        return data
