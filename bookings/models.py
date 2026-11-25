import secrets
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db import models
from django.utils import timezone

from tenants.models import TenantManager, TenantModel

REFERENCE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I confusion


class Extra(TenantModel):
    """Add-ons sold with a booking: racket rental, balls, coaching, drinks..."""

    name = models.CharField(max_length=60)
    description = models.CharField(max_length=140, blank=True)
    price = models.DecimalField(max_digits=10, decimal_places=2)
    max_quantity = models.PositiveSmallIntegerField(default=4)
    is_active = models.BooleanField(default=True)
    sort_order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "name"]

    def __str__(self):
        return self.name


class PromoCode(TenantModel):
    class DiscountType(models.TextChoices):
        PERCENT = "percent", "Percent (%)"
        FIXED = "fixed", "Fixed amount"

    code = models.CharField(max_length=30)
    description = models.CharField(max_length=140, blank=True)
    discount_type = models.CharField(max_length=10, choices=DiscountType.choices, default=DiscountType.PERCENT)
    value = models.DecimalField(max_digits=10, decimal_places=2)
    min_amount = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0"))
    valid_from = models.DateField(null=True, blank=True)
    valid_until = models.DateField(null=True, blank=True)
    max_uses = models.PositiveIntegerField(null=True, blank=True, help_text="Leave empty for unlimited")
    times_used = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [models.UniqueConstraint(fields=["club", "code"], name="promo_code_per_club")]

    def __str__(self):
        return self.code

    def save(self, *args, **kwargs):
        self.code = self.code.strip().upper()
        super().save(*args, **kwargs)

    def problem(self, amount, on_date):
        """Return a human readable reason the code can't be used, or None if it's valid."""
        if not self.is_active:
            return "This promo code is no longer active."
        if self.valid_from and on_date < self.valid_from:
            return "This promo code isn't valid for that date yet."
        if self.valid_until and on_date > self.valid_until:
            return "This promo code has expired."
        if self.max_uses is not None and self.times_used >= self.max_uses:
            return "This promo code has reached its usage limit."
        if amount < self.min_amount:
            return f"This promo code needs a minimum spend of {self.min_amount:,.0f}."
        return None

    def discount_for(self, amount: Decimal) -> Decimal:
        if self.discount_type == self.DiscountType.PERCENT:
            discount = amount * self.value / Decimal("100")
        else:
            discount = self.value
        return min(discount, amount).quantize(Decimal("0.01"))


class BookingSeries(TenantModel):
    """A weekly standing booking, e.g. the same group every Tuesday at 8 PM."""

    court = models.ForeignKey("courts.Court", on_delete=models.PROTECT, related_name="+")
    customer_name = models.CharField(max_length=120)
    customer_phone = models.CharField(max_length=20)
    weekday = models.PositiveSmallIntegerField()
    start_time = models.TimeField()
    duration_minutes = models.PositiveSmallIntegerField()
    first_date = models.DateField()
    weeks = models.PositiveSmallIntegerField()
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.customer_name} · {self.get_weekday_display()} {self.start_time:%H:%M}"

    WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

    def get_weekday_display(self):
        return self.WEEKDAYS[self.weekday]


class BookingQuerySet(models.QuerySet):
    def active(self, now=None):
        """Bookings that occupy their court. An unpaid hold stops counting the moment it lapses,
        even before the expiry job has run."""
        now = now or timezone.now()
        return self.exclude(status__in=Booking.INACTIVE_STATUSES).exclude(
            status=Booking.Status.AWAITING_PAYMENT, payment_due_at__lt=now
        )

    def overlapping(self, court, start_at, end_at):
        return self.active().filter(court=court, start_at__lt=end_at, end_at__gt=start_at)


class Booking(TenantModel):
    class Status(models.TextChoices):
        AWAITING_PAYMENT = "awaiting_payment", "Awaiting payment"
        PENDING = "pending", "Pending"
        CONFIRMED = "confirmed", "Confirmed"
        CHECKED_IN = "checked_in", "Checked in"
        COMPLETED = "completed", "Completed"
        CANCELLED = "cancelled", "Cancelled"
        NO_SHOW = "no_show", "No-show"
        EXPIRED = "expired", "Expired (unpaid)"

    class Source(models.TextChoices):
        ONLINE = "online", "Online"
        WALK_IN = "walk_in", "Walk-in"
        PHONE = "phone", "Phone / WhatsApp"

    # Statuses that free the court again
    INACTIVE_STATUSES = [Status.CANCELLED, Status.NO_SHOW, Status.EXPIRED]

    reference = models.CharField(max_length=12, unique=True, editable=False)
    court = models.ForeignKey("courts.Court", on_delete=models.PROTECT, related_name="bookings")
    customer = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="bookings"
    )
    customer_name = models.CharField(max_length=120)
    customer_phone = models.CharField(max_length=20, db_index=True)
    customer_email = models.EmailField(blank=True)

    business_date = models.DateField(db_index=True, help_text="The club day this booking belongs to")
    start_at = models.DateTimeField(db_index=True)
    end_at = models.DateTimeField()
    duration_minutes = models.PositiveSmallIntegerField()

    court_amount = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0"))
    extras_amount = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0"))
    discount_amount = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0"))
    promo_code = models.ForeignKey(PromoCode, null=True, blank=True, on_delete=models.SET_NULL)
    series = models.ForeignKey(BookingSeries, null=True, blank=True, on_delete=models.SET_NULL, related_name="bookings")
    prepayment_amount = models.DecimalField(
        max_digits=10, decimal_places=2, default=Decimal("0"), help_text="Paid online to confirm (deposit or full)"
    )
    payment_due_at = models.DateTimeField(null=True, blank=True, help_text="Unpaid hold is released after this")

    status = models.CharField(max_length=20, choices=Status.choices, default=Status.CONFIRMED, db_index=True)
    source = models.CharField(max_length=20, choices=Source.choices, default=Source.ONLINE)
    players = models.PositiveSmallIntegerField(default=4)
    notes = models.TextField(blank=True)
    cancel_reason = models.CharField(max_length=200, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = TenantManager.from_queryset(BookingQuerySet)()
    all_clubs = models.Manager.from_queryset(BookingQuerySet)()

    class Meta:
        ordering = ["-start_at"]
        indexes = [models.Index(fields=["court", "start_at", "end_at"])]

    def __str__(self):
        return f"{self.reference} · {self.court} · {timezone.localtime(self.start_at):%d %b %H:%M}"

    def save(self, *args, **kwargs):
        if not self.reference:
            self.reference = self.generate_reference()
        if self.start_at and self.duration_minutes and not self.end_at:
            self.end_at = self.start_at + timedelta(minutes=self.duration_minutes)
        super().save(*args, **kwargs)

    @classmethod
    def generate_reference(cls):
        while True:
            ref = "PB" + "".join(secrets.choice(REFERENCE_ALPHABET) for _ in range(6))
            if not cls.objects.filter(reference=ref).exists():
                return ref

    @property
    def subtotal(self):
        return self.court_amount + self.extras_amount

    @property
    def total(self):
        return max(self.subtotal - self.discount_amount, Decimal("0"))

    @property
    def is_active(self):
        if self.status == self.Status.AWAITING_PAYMENT:
            return self.is_hold_open()
        return self.status not in self.INACTIVE_STATUSES

    @property
    def is_upcoming(self):
        return self.is_active and self.start_at > timezone.now()

    def customer_can_cancel(self, cancellation_hours, now=None):
        now = now or timezone.now()
        if self.is_hold_open(now):
            return True  # an unpaid hold can always be released
        return (
            self.status in (self.Status.AWAITING_PAYMENT, self.Status.PENDING, self.Status.CONFIRMED)
            and self.start_at - now >= timedelta(hours=cancellation_hours)
        )

    def is_hold_open(self, now=None):
        return (
            self.status == self.Status.AWAITING_PAYMENT
            and self.payment_due_at is not None
            and self.payment_due_at >= (now or timezone.now())
        )

    @property
    def awaiting_payment(self):
        """Unpaid hold that is still running."""
        return self.is_hold_open()

    @property
    def hold_expired(self):
        return (
            self.status == self.Status.AWAITING_PAYMENT
            and self.payment_due_at is not None
            and self.payment_due_at < timezone.now()
        )

    @property
    def hold_seconds_left(self):
        if not self.is_hold_open():
            return 0
        return int((self.payment_due_at - timezone.now()).total_seconds())

    @property
    def status_color(self):
        return {
            "awaiting_payment": "amber",
            "pending": "amber",
            "expired": "slate",
            "confirmed": "green",
            "checked_in": "blue",
            "completed": "slate",
            "cancelled": "red",
            "no_show": "red",
            "expired": "slate",
        }.get(self.status, "slate")


class BookingExtra(models.Model):
    booking = models.ForeignKey(Booking, on_delete=models.CASCADE, related_name="extras")
    extra = models.ForeignKey(Extra, on_delete=models.PROTECT)
    quantity = models.PositiveSmallIntegerField(default=1)
    unit_price = models.DecimalField(max_digits=10, decimal_places=2)

    def __str__(self):
        return f"{self.quantity} × {self.extra}"

    @property
    def line_total(self):
        return self.unit_price * self.quantity


class Notification(models.Model):
    """Log of every automatic message — makes sending idempotent and visible to the owner."""

    class Kind(models.TextChoices):
        CONFIRMATION = "confirmation", "Booking confirmation"
        REMINDER_24H = "reminder_24h", "Reminder (24h)"
        REMINDER_2H = "reminder_2h", "Reminder (2h)"

    class Channel(models.TextChoices):
        WHATSAPP = "whatsapp", "WhatsApp"
        EMAIL = "email", "Email"

    class State(models.TextChoices):
        SENT = "sent", "Sent"
        FAILED = "failed", "Failed"

    MAX_ATTEMPTS = 3

    booking = models.ForeignKey(Booking, on_delete=models.CASCADE, related_name="notifications")
    kind = models.CharField(max_length=20, choices=Kind.choices)
    channel = models.CharField(max_length=10, choices=Channel.choices)
    state = models.CharField(max_length=10, choices=State.choices, default=State.FAILED)
    attempts = models.PositiveSmallIntegerField(default=0)
    provider_id = models.CharField(max_length=120, blank=True)
    error = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["created_at"]
        constraints = [models.UniqueConstraint(fields=["booking", "kind", "channel"], name="one_notification_per_kind")]

    def __str__(self):
        return f"{self.get_kind_display()} via {self.get_channel_display()} ({self.state})"
