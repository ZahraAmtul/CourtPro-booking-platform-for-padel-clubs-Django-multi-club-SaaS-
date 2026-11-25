import uuid
from decimal import Decimal

from django.conf import settings
from django.db import models
from django.db.models import Sum
from django.utils import timezone

from tenants.models import TenantModel

CENT = Decimal("0.01")


class InvoiceSequence(TenantModel):
    """Gap-free yearly invoice counter per club (row locked while issuing a number)."""

    year = models.PositiveIntegerField()
    last_number = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["club", "year"], name="invoice_sequence_per_club")]

    def __str__(self):
        return f"{self.year}: {self.last_number}"


class Invoice(TenantModel):
    class Status(models.TextChoices):
        UNPAID = "unpaid", "Unpaid"
        PARTIAL = "partial", "Partially paid"
        PAID = "paid", "Paid"
        VOID = "void", "Void"

    number = models.CharField(max_length=30, editable=False)
    token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    booking = models.OneToOneField(
        "bookings.Booking", null=True, blank=True, on_delete=models.SET_NULL, related_name="invoice"
    )
    customer = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="invoices"
    )
    bill_to_name = models.CharField(max_length=120)
    bill_to_phone = models.CharField(max_length=20, blank=True)
    bill_to_email = models.EmailField(blank=True)

    issue_date = models.DateField(default=timezone.localdate)
    due_date = models.DateField(default=timezone.localdate)

    subtotal = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))
    discount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))
    tax_percent = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("0"))
    tax_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))
    total = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))
    amount_paid = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))

    status = models.CharField(max_length=10, choices=Status.choices, default=Status.UNPAID, db_index=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [models.UniqueConstraint(fields=["club", "number"], name="invoice_number_per_club")]

    def __str__(self):
        return self.number

    @property
    def balance(self):
        if self.status == self.Status.VOID:
            return Decimal("0")
        return max(self.total - self.amount_paid, Decimal("0"))

    @property
    def is_overdue(self):
        return self.status in (self.Status.UNPAID, self.Status.PARTIAL) and self.due_date < timezone.localdate()

    @property
    def status_color(self):
        return {"unpaid": "amber", "partial": "blue", "paid": "green", "void": "slate"}[self.status]

    def recalculate(self, save=True):
        """Recompute totals from items & payments and derive the status."""
        self.subtotal = sum((item.amount for item in self.items.all()), Decimal("0"))
        self.discount = min(self.discount, self.subtotal)
        taxable = self.subtotal - self.discount
        self.tax_amount = (taxable * self.tax_percent / Decimal("100")).quantize(CENT)
        self.total = (taxable + self.tax_amount).quantize(CENT)
        self.amount_paid = self.payments.aggregate(s=Sum("amount"))["s"] or Decimal("0")
        if self.status != self.Status.VOID:
            if self.amount_paid >= self.total:
                self.status = self.Status.PAID
            elif self.amount_paid > 0:
                self.status = self.Status.PARTIAL
            else:
                self.status = self.Status.UNPAID
        if save:
            self.save()
        return self


class InvoiceItem(models.Model):
    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name="items")
    description = models.CharField(max_length=200)
    quantity = models.DecimalField(max_digits=8, decimal_places=2, default=Decimal("1"))
    unit_price = models.DecimalField(max_digits=12, decimal_places=2)

    class Meta:
        ordering = ["pk"]

    def __str__(self):
        return self.description

    @property
    def amount(self):
        return (self.quantity * self.unit_price).quantize(CENT)


class Payment(TenantModel):
    class Method(models.TextChoices):
        CASH = "cash", "Cash"
        CARD = "card", "Card"
        BANK = "bank_transfer", "Bank transfer"
        JAZZCASH = "jazzcash", "JazzCash"
        EASYPAISA = "easypaisa", "Easypaisa"
        ONLINE = "online", "Online gateway"
        WALLET = "wallet", "Club credit"

    # Money that actually came in. Club credit was already collected when it was bought.
    CASH_IN_METHODS = [Method.CASH, Method.CARD, Method.BANK, Method.JAZZCASH, Method.EASYPAISA, Method.ONLINE]

    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name="payments")
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    method = models.CharField(max_length=20, choices=Method.choices, default=Method.CASH)
    reference = models.CharField(max_length=80, blank=True, help_text="Transaction ID / receipt no.")
    paid_at = models.DateTimeField(default=timezone.now, db_index=True)
    received_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    note = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["-paid_at"]

    def __str__(self):
        return f"{self.amount} via {self.get_method_display()}"


class OnlinePayment(TenantModel):
    """One checkout attempt with a payment gateway. Its reference is sent to the gateway and
    makes the callback idempotent (a retried callback can never record the payment twice)."""

    class State(models.TextChoices):
        STARTED = "started", "Started"
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"

    reference = models.CharField(max_length=40, unique=True)
    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name="online_payments")
    gateway = models.CharField(max_length=20)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    state = models.CharField(max_length=10, choices=State.choices, default=State.STARTED, db_index=True)
    response_code = models.CharField(max_length=20, blank=True)
    response_message = models.CharField(max_length=255, blank=True)
    raw_response = models.JSONField(default=dict, blank=True)
    payment = models.OneToOneField(Payment, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.reference} {self.amount} ({self.state})"


# --------------------------------------------------------------------------- prepaid credit
class CreditPackage(TenantModel):
    """Something the club sells upfront, e.g. pay Rs 45,000 and get Rs 50,000 of credit."""

    name = models.CharField(max_length=60)
    price = models.DecimalField("Customer pays", max_digits=10, decimal_places=2)
    credit = models.DecimalField("Credit added", max_digits=10, decimal_places=2)
    description = models.CharField(max_length=140, blank=True)
    is_active = models.BooleanField(default=True)
    sort_order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "price"]

    def __str__(self):
        return self.name

    @property
    def bonus(self):
        return max(self.credit - self.price, Decimal("0"))


class Wallet(TenantModel):
    """Prepaid club credit for one customer, identified by mobile number (players often book as guests)."""

    phone = models.CharField(max_length=20, help_text="Normalised, e.g. 923001234567")
    name = models.CharField(max_length=120)
    balance = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(fields=["club", "phone"], name="one_wallet_per_phone"),
            models.CheckConstraint(condition=models.Q(balance__gte=0), name="wallet_never_negative"),
        ]

    def __str__(self):
        return f"{self.name} ({self.balance})"


class WalletTransaction(TenantModel):
    class Kind(models.TextChoices):
        PURCHASE = "purchase", "Credit bought"
        SPEND = "spend", "Used for booking"
        REFUND = "refund", "Refund to credit"
        ADJUST = "adjust", "Manual adjustment"

    wallet = models.ForeignKey(Wallet, on_delete=models.CASCADE, related_name="transactions")
    kind = models.CharField(max_length=10, choices=Kind.choices)
    amount = models.DecimalField(max_digits=12, decimal_places=2, help_text="+ adds credit, − uses it")
    balance_after = models.DecimalField(max_digits=12, decimal_places=2)
    invoice = models.ForeignKey(Invoice, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    note = models.CharField(max_length=200, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-pk"]

    def __str__(self):
        return f"{self.get_kind_display()} {self.amount}"


# --------------------------------------------------------------------------- end-of-day cash count
class DayClose(TenantModel):
    """Reception's end-of-day cash count for one business day, kept for the owner to review."""

    business_date = models.DateField()
    expected_cash = models.DecimalField(max_digits=12, decimal_places=2)
    counted_cash = models.DecimalField(max_digits=12, decimal_places=2)
    totals = models.JSONField(default=dict, help_text="Takings by payment method at closing time")
    note = models.CharField(max_length=300, blank=True)
    closed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    closed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-business_date"]
        constraints = [models.UniqueConstraint(fields=["club", "business_date"], name="one_close_per_day")]

    def __str__(self):
        return f"Close {self.business_date}"

    @property
    def difference(self):
        return self.counted_cash - self.expected_cash
