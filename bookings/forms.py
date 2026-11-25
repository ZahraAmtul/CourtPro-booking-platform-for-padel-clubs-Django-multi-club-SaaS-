import re

from django import forms

from courts.models import Court

from .models import Booking, Extra

PHONE_RE = re.compile(r"^\+?[\d\s\-()]{10,18}$")


def clean_phone_value(value):
    value = (value or "").strip()
    if not PHONE_RE.match(value) or len(re.sub(r"\D", "", value)) < 10:
        raise forms.ValidationError("Enter a valid phone number, e.g. 0300 1234567")
    return value


class ExtrasMixin:
    """Adds one quantity field per active Extra: extra_<id>."""

    def add_extra_fields(self):
        self.extra_items = list(Extra.objects.filter(is_active=True))
        for extra in self.extra_items:
            self.fields[f"extra_{extra.pk}"] = forms.IntegerField(
                label=extra.name, min_value=0, max_value=extra.max_quantity, initial=0, required=False,
                widget=forms.NumberInput(attrs={"data-price": str(extra.price), "class": "qty-input"}),
            )

    def extra_fields(self):
        return [(extra, self[f"extra_{extra.pk}"]) for extra in self.extra_items]

    def selected_extras(self):
        return [
            (extra, self.cleaned_data.get(f"extra_{extra.pk}") or 0)
            for extra in self.extra_items
            if self.cleaned_data.get(f"extra_{extra.pk}")
        ]


class BookingDetailsForm(ExtrasMixin, forms.Form):
    customer_name = forms.CharField(label="Full name", max_length=120)
    customer_phone = forms.CharField(label="Mobile number", max_length=20)
    customer_email = forms.EmailField(label="Email (for confirmation)", required=False)
    players = forms.TypedChoiceField(choices=[(2, "2 players"), (4, "4 players")], coerce=int, initial=4)
    promo_code = forms.CharField(max_length=30, required=False)
    notes = forms.CharField(widget=forms.Textarea(attrs={"rows": 2}), required=False, max_length=500)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.add_extra_fields()
        self.fields["customer_phone"].widget.attrs.update({"placeholder": "0300 1234567", "inputmode": "tel"})
        self.fields["promo_code"].widget.attrs.update({"placeholder": "Optional", "autocomplete": "off"})

    def clean_customer_phone(self):
        return clean_phone_value(self.cleaned_data["customer_phone"])


class ManageBookingForm(forms.Form):
    reference = forms.CharField(max_length=12, widget=forms.TextInput(attrs={"placeholder": "PBXXXXXX"}))
    phone = forms.CharField(label="Mobile number used for booking", max_length=20)

    def clean_reference(self):
        return self.cleaned_data["reference"].strip().upper()


class StaffBookingForm(ExtrasMixin, forms.Form):
    court = forms.ModelChoiceField(queryset=Court.objects.filter(is_active=True), empty_label=None)
    business_date = forms.DateField(label="Date", widget=forms.DateInput(attrs={"type": "date"}))
    start_time = forms.TimeField(widget=forms.TimeInput(attrs={"type": "time", "step": 1800}))
    duration = forms.TypedChoiceField(coerce=int)
    customer_name = forms.CharField(max_length=120)
    customer_phone = forms.CharField(max_length=20)
    customer_email = forms.EmailField(required=False)
    source = forms.ChoiceField(choices=[c for c in Booking.Source.choices if c[0] != "online"])
    players = forms.TypedChoiceField(choices=[(2, "2"), (4, "4")], coerce=int, initial=4)
    promo_code = forms.CharField(max_length=30, required=False)
    notes = forms.CharField(widget=forms.Textarea(attrs={"rows": 2}), required=False)
    repeat_weeks = forms.TypedChoiceField(
        label="Repeat weekly", coerce=int, initial=0, required=False,
        choices=[(0, "No — one-off booking"), (4, "Every week for 4 weeks"), (8, "Every week for 8 weeks"),
                 (12, "Every week for 12 weeks"), (26, "Every week for 26 weeks")],
        help_text="Same court and time each week. Weeks that are already taken are skipped.",
    )
    collect_payment = forms.BooleanField(required=False, label="Customer paid in full now",
                                         help_text="For weekly bookings this pays the first week.")
    payment_method = forms.ChoiceField(
        choices=[("cash", "Cash"), ("card", "Card"), ("bank_transfer", "Bank transfer"),
                 ("jazzcash", "JazzCash"), ("easypaisa", "Easypaisa")],
        required=False,
    )

    def __init__(self, *args, slot_minutes=30, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["duration"].choices = [(m, f"{m} min") for m in range(slot_minutes, 241, slot_minutes)]
        self.fields["duration"].initial = 60 if 60 % slot_minutes == 0 else slot_minutes
        self.fields["start_time"].widget.attrs["step"] = slot_minutes * 60
        self.add_extra_fields()

    def clean_customer_phone(self):
        return clean_phone_value(self.cleaned_data["customer_phone"])
