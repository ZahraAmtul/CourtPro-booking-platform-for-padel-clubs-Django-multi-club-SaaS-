from datetime import time
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import models


class SiteSettings(models.Model):
    """Everything a club owner can configure — one row per club."""

    club = models.OneToOneField("tenants.Club", on_delete=models.CASCADE, related_name="settings")

    SLOT_CHOICES = [(30, "30 minutes"), (60, "60 minutes")]

    # Branding & contact
    club_name = models.CharField(max_length=80, default="Smash Padel Club")
    tagline = models.CharField(max_length=160, default="Book your court in under 30 seconds.")
    phone = models.CharField(max_length=30, blank=True, default="0300 1234567")
    whatsapp = models.CharField(
        max_length=20, blank=True, default="923001234567",
        help_text="International format without + or spaces, e.g. 923001234567",
    )
    email = models.EmailField(blank=True, default="hello@smashpadel.pk")
    address = models.CharField(max_length=200, blank=True, default="Plot 12, DHA Phase 6, Lahore")
    map_url = models.URLField(blank=True)
    instagram_url = models.URLField(blank=True)

    # Money
    currency_symbol = models.CharField(max_length=8, default="Rs")
    tax_percent = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("0"))
    tax_label = models.CharField(max_length=30, default="Sales tax")

    # Opening hours (close earlier than open = closes after midnight)
    open_time = models.TimeField(default=time(8, 0))
    close_time = models.TimeField(default=time(2, 0))
    slot_minutes = models.PositiveSmallIntegerField(choices=SLOT_CHOICES, default=30)
    allowed_durations = models.CharField(
        max_length=40, default="60,90,120", help_text="Comma separated minutes customers can book"
    )

    # Peak pricing window (may also cross midnight)
    peak_start = models.TimeField(default=time(17, 0))
    peak_end = models.TimeField(default=time(1, 0))
    weekend_all_day_peak = models.BooleanField(default=True, help_text="Saturday & Sunday charged at peak rate")

    # Booking policy
    booking_window_days = models.PositiveSmallIntegerField(default=14)
    min_notice_minutes = models.PositiveSmallIntegerField(default=30)
    cancellation_hours = models.PositiveSmallIntegerField(
        default=6, help_text="Customers can self-cancel up to this many hours before start"
    )
    auto_confirm = models.BooleanField(
        default=True, help_text="Confirm online bookings instantly. Off = owner approves each booking."
    )

    # Invoices
    invoice_prefix = models.CharField(max_length=10, default="INV")
    invoice_due_days = models.PositiveSmallIntegerField(default=0)
    invoice_footer = models.TextField(blank=True, default="Thank you for playing with us! See you on court.")
    payment_instructions = models.TextField(
        blank=True, default="Pay at reception (cash/card) or via bank transfer / JazzCash / Easypaisa."
    )

    # Online payments (advance payment to confirm a booking)
    PREPAY_CHOICES = [("none", "No — pay at the club"), ("deposit", "Deposit to confirm"), ("full", "Full amount to confirm")]
    GATEWAY_CHOICES = [("", "Not connected"), ("jazzcash", "JazzCash (wallet / card)"), ("demo", "Demo gateway (testing only)")]
    prepayment_mode = models.CharField(max_length=10, choices=PREPAY_CHOICES, default="none")
    deposit_percent = models.PositiveSmallIntegerField(default=50, help_text="Used when a deposit is required")
    payment_hold_minutes = models.PositiveSmallIntegerField(
        default=15, help_text="How long an unpaid online booking holds the slot before it's released"
    )
    payment_gateway = models.CharField(max_length=20, choices=GATEWAY_CHOICES, blank=True, default="")
    jazzcash_merchant_id = models.CharField(max_length=40, blank=True)
    jazzcash_password = models.CharField(max_length=120, blank=True)
    jazzcash_integrity_salt = models.CharField(max_length=120, blank=True)
    jazzcash_sandbox = models.BooleanField(default=True, help_text="Untick when you receive live credentials")

    # WhatsApp Cloud API — the club's own WhatsApp Business number, so Meta bills the club directly
    whatsapp_enabled = models.BooleanField(default=False, help_text="Send automatic confirmations & reminders")
    whatsapp_phone_number_id = models.CharField(max_length=40, blank=True)
    whatsapp_access_token = models.TextField(blank=True)
    whatsapp_template_language = models.CharField(max_length=10, default="en")
    whatsapp_confirmation_template = models.CharField(max_length=60, default="booking_confirmed")
    whatsapp_reminder_template = models.CharField(max_length=60, default="booking_reminder")
    remind_24h = models.BooleanField("Reminder 24 hours before", default=True)
    remind_2h = models.BooleanField("Reminder 2 hours before", default=True)

    class Meta:
        verbose_name = "Club settings"
        verbose_name_plural = "Club settings"

    def __str__(self):
        return self.club_name

    def clean(self):
        try:
            durations = self.duration_list
        except ValueError:
            raise ValidationError({"allowed_durations": "Use numbers separated by commas, e.g. 60,90,120"})
        if not durations or any(d % self.slot_minutes for d in durations):
            raise ValidationError({"allowed_durations": "Each duration must be a multiple of the slot length."})
        if self.prepayment_mode == "deposit" and not (0 < self.deposit_percent <= 100):
            raise ValidationError({"deposit_percent": "Deposit must be between 1 and 100%."})
        if self.payment_gateway == "jazzcash" and not (
            self.jazzcash_merchant_id and self.jazzcash_password and self.jazzcash_integrity_salt
        ):
            raise ValidationError({"payment_gateway": "Enter your JazzCash merchant ID, password and integrity salt."})
        if self.whatsapp_enabled and not (self.whatsapp_phone_number_id and self.whatsapp_access_token):
            raise ValidationError({"whatsapp_enabled": "Enter the WhatsApp phone number ID and access token first."})
        if self.open_time == self.close_time:
            raise ValidationError({"close_time": "Closing time must differ from opening time."})

    @classmethod
    def load(cls, club=None):
        """Settings of the given club, or of the club the current request/job is for."""
        from tenants.context import get_current_club

        club = club or get_current_club()
        if club is None:
            raise LookupError("SiteSettings.load() needs an active club")
        cached = getattr(club, "_settings_cache", None)
        if cached is None:
            cached, _ = cls.objects.get_or_create(club=club, defaults={"club_name": club.name})
            club._settings_cache = cached
        return cached

    @property
    def duration_list(self):
        return sorted({int(x) for x in self.allowed_durations.split(",") if x.strip()})

    @property
    def closes_after_midnight(self):
        return self.close_time <= self.open_time

    @property
    def gateway_ready(self):
        from django.conf import settings

        if self.payment_gateway == "jazzcash":
            return bool(self.jazzcash_merchant_id and self.jazzcash_password and self.jazzcash_integrity_salt)
        if self.payment_gateway == "demo":
            return settings.PAYMENTS_DEMO_ENABLED
        return False

    @property
    def prepayment_active(self):
        return self.prepayment_mode != "none" and self.gateway_ready

    @property
    def whatsapp_ready(self):
        return self.whatsapp_enabled and bool(self.whatsapp_phone_number_id and self.whatsapp_access_token)

    @property
    def whatsapp_url(self):
        return f"https://wa.me/{self.whatsapp}" if self.whatsapp else ""
