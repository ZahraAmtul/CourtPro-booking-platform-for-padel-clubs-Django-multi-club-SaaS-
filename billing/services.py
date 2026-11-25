from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from core.models import SiteSettings

from .models import Invoice, InvoiceItem, InvoiceSequence, Payment


@transaction.atomic
def next_invoice_number(prefix: str) -> str:
    year = timezone.localdate().year
    seq, _ = InvoiceSequence.objects.select_for_update().get_or_create(year=year)
    seq.last_number += 1
    seq.save(update_fields=["last_number"])
    return f"{prefix}-{year}-{seq.last_number:05d}"


@transaction.atomic
def create_invoice(*, bill_to_name, items, bill_to_phone="", bill_to_email="", discount=Decimal("0"),
                   customer=None, booking=None, notes="", due_date=None, tax_percent=None) -> Invoice:
    """items: iterable of (description, quantity, unit_price)."""
    s = SiteSettings.load()
    today = timezone.localdate()
    invoice = Invoice.objects.create(
        number=next_invoice_number(s.invoice_prefix),
        booking=booking,
        customer=customer,
        bill_to_name=bill_to_name,
        bill_to_phone=bill_to_phone,
        bill_to_email=bill_to_email,
        issue_date=today,
        due_date=due_date or today + timedelta(days=s.invoice_due_days),
        discount=discount or Decimal("0"),
        tax_percent=s.tax_percent if tax_percent is None else tax_percent,
        notes=notes,
    )
    InvoiceItem.objects.bulk_create(
        [InvoiceItem(invoice=invoice, description=d, quantity=q, unit_price=p) for d, q, p in items]
    )
    return invoice.recalculate()


def create_invoice_for_booking(booking, s=None) -> Invoice:
    s = s or SiteSettings.load()
    start = timezone.localtime(booking.start_at)
    end = timezone.localtime(booking.end_at)
    items = [(
        f"{booking.court.name} · {start:%a %d %b %Y}, {start:%I:%M %p}–{end:%I:%M %p} "
        f"({booking.duration_minutes} min)",
        Decimal("1"),
        booking.court_amount,
    )]
    for be in booking.extras.select_related("extra"):
        items.append((be.extra.name, Decimal(be.quantity), be.unit_price))
    notes = f"Promo {booking.promo_code.code} applied" if booking.promo_code_id else ""
    due = max(timezone.localdate(), start.date()) if s.invoice_due_days == 0 else None
    return create_invoice(
        bill_to_name=booking.customer_name,
        bill_to_phone=booking.customer_phone,
        bill_to_email=booking.customer_email,
        items=items,
        discount=booking.discount_amount,
        customer=booking.customer,
        booking=booking,
        notes=notes,
        due_date=due,
    )


@transaction.atomic
def record_payment(invoice: Invoice, *, amount, method=Payment.Method.CASH, reference="", received_by=None,
                   note="", allow_overpay=False) -> Payment:
    """Record money received. `allow_overpay` is for gateway callbacks: money that has already
    moved must be recorded even if the club took cash for the same invoice in the meantime."""
    from bookings.models import Booking

    invoice = Invoice.objects.select_for_update().get(pk=invoice.pk)
    amount = Decimal(amount)
    if invoice.status == Invoice.Status.VOID and not allow_overpay:
        raise ValidationError("You can't record a payment on a void invoice.")
    if amount <= 0:
        raise ValidationError("Payment amount must be greater than zero.")
    if amount > invoice.balance and not allow_overpay:
        raise ValidationError(f"Payment is more than the balance due ({invoice.balance:,.2f}).")
    payment = Payment.objects.create(
        invoice=invoice, amount=amount, method=method, reference=reference, received_by=received_by, note=note
    )
    invoice.recalculate()
    booking = invoice.booking
    if booking and booking.status in (Booking.Status.AWAITING_PAYMENT, Booking.Status.EXPIRED):
        from bookings.services import confirm_after_payment

        confirm_after_payment(booking)
    elif booking and invoice.status == Invoice.Status.PAID and booking.status == Booking.Status.PENDING:
        booking.status = Booking.Status.CONFIRMED
        booking.save(update_fields=["status", "updated_at"])
    return payment


@transaction.atomic
def void_invoice(invoice: Invoice) -> Invoice:
    invoice = Invoice.objects.select_for_update().get(pk=invoice.pk)
    if invoice.amount_paid > 0:
        raise ValidationError("This invoice has payments recorded. Refund them before voiding.")
    invoice.status = Invoice.Status.VOID
    invoice.save(update_fields=["status", "updated_at"])
    return invoice


def void_invoice_if_unpaid(booking) -> None:
    invoice = Invoice.objects.filter(booking=booking).first()
    if invoice and invoice.amount_paid == 0 and invoice.status != Invoice.Status.VOID:
        void_invoice(invoice)
