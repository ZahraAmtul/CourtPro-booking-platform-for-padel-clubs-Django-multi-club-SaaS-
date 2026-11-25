"""Prepaid club credit: sell packages, pay invoices from credit, refund cancelled bookings to credit.

Credit is bought with real money (recorded as an invoice + payment at purchase time). Spending it
records a "Club credit" payment on the booking's invoice, which is NOT counted as money collected —
that money already came in when the credit was sold."""
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction

from bookings.notifications import normalize_phone

from .models import Invoice, Payment, Wallet, WalletTransaction
from .services import create_invoice, record_payment


def find_wallet(phone, lock=False):
    qs = Wallet.objects.select_for_update() if lock else Wallet.objects
    return qs.filter(phone=normalize_phone(phone)).first()


def _wallet_for_update(phone, name):
    phone = normalize_phone(phone)
    if not phone:
        raise ValidationError("A mobile number is needed for club credit.")
    wallet, _ = Wallet.objects.get_or_create(phone=phone, defaults={"name": name or phone})
    return Wallet.objects.select_for_update().get(pk=wallet.pk)


def _move(wallet, amount, kind, *, invoice=None, note="", user=None):
    new_balance = wallet.balance + amount
    if new_balance < 0:
        raise ValidationError(f"Not enough credit (balance {wallet.balance:,.0f}).")
    wallet.balance = new_balance
    wallet.save(update_fields=["balance", "updated_at"])
    return WalletTransaction.objects.create(
        wallet=wallet, kind=kind, amount=amount, balance_after=new_balance, invoice=invoice, note=note[:200],
        created_by=user,
    )


@transaction.atomic
def sell_credit(*, phone, name, method, received_by=None, package=None, price=None, credit=None, reference=""):
    """Customer pays `price` (or the package price) and gets `credit` added to their wallet."""
    if package:
        price, credit, label = package.price, package.credit, f"Club credit — {package.name}"
    else:
        price = Decimal(price or 0)
        credit = Decimal(credit or price)
        label = "Club credit top-up"
    if price <= 0 or credit <= 0:
        raise ValidationError("Enter the amount paid and the credit to add.")
    if method == Payment.Method.WALLET:
        raise ValidationError("Credit can't be bought with credit.")
    wallet = _wallet_for_update(phone, name)
    invoice = create_invoice(
        bill_to_name=name or wallet.name, bill_to_phone=phone,
        items=[(f"{label} ({credit:,.0f} credit)", Decimal("1"), price)],
        tax_percent=Decimal("0"),  # tax is charged on the bookings the credit pays for
        notes="Prepaid club credit",
    )
    record_payment(invoice, amount=price, method=method, reference=reference, received_by=received_by)
    _move(wallet, credit, WalletTransaction.Kind.PURCHASE, invoice=invoice, user=received_by,
          note=f"Paid {price:,.0f} by {Payment.Method(method).label}")
    invoice.refresh_from_db()
    return wallet, invoice


@transaction.atomic
def pay_with_credit(invoice: Invoice, *, received_by=None, amount=None) -> Payment:
    invoice = Invoice.objects.select_for_update().get(pk=invoice.pk)
    wallet = find_wallet(invoice.bill_to_phone, lock=True)
    if not wallet or wallet.balance <= 0:
        raise ValidationError("This customer has no club credit.")
    amount = min(Decimal(amount) if amount else invoice.balance, invoice.balance, wallet.balance)
    if amount <= 0:
        raise ValidationError("Nothing to pay on this invoice.")
    payment = record_payment(invoice, amount=amount, method=Payment.Method.WALLET,
                             reference=f"CREDIT-{wallet.pk}", received_by=received_by, note="Paid from club credit")
    _move(wallet, -amount, WalletTransaction.Kind.SPEND, invoice=invoice, user=received_by,
          note=f"Invoice {invoice.number}" + (f" · booking {invoice.booking.reference}" if invoice.booking else ""))
    return payment


@transaction.atomic
def refund_booking_to_credit(booking, *, user=None):
    """For a cancelled/expired booking that was paid: turn the payment into club credit."""
    from bookings.models import Booking

    booking = Booking.objects.select_for_update().select_related("invoice").get(pk=booking.pk)
    if booking.status not in (Booking.Status.CANCELLED, Booking.Status.EXPIRED):
        raise ValidationError("Only cancelled bookings can be refunded to credit.")
    if "[refund handled]" in booking.notes:
        raise ValidationError("This refund was already handled.")
    amount = booking.invoice.amount_paid
    if amount <= 0:
        raise ValidationError("Nothing was paid for this booking.")
    wallet = _wallet_for_update(booking.customer_phone, booking.customer_name)
    _move(wallet, amount, WalletTransaction.Kind.REFUND, invoice=booking.invoice, user=user,
          note=f"Cancelled booking {booking.reference}")
    booking.notes = (booking.notes + f"\n[refund handled] {amount:,.0f} added to club credit").strip()
    booking.save(update_fields=["notes", "updated_at"])
    return wallet


@transaction.atomic
def adjust_credit(wallet, amount, note, user=None):
    wallet = Wallet.objects.select_for_update().get(pk=wallet.pk)
    if not note:
        raise ValidationError("Add a reason for the adjustment.")
    return _move(wallet, Decimal(amount), WalletTransaction.Kind.ADJUST, note=note, user=user)
