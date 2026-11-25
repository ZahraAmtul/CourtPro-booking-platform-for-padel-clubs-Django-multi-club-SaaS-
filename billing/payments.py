"""Online payment flow: start a checkout attempt, then apply the gateway's verified result."""
import secrets
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from .gateways import GatewayResult
from .models import Invoice, OnlinePayment
from .services import record_payment


def amount_options(invoice: Invoice):
    """What the customer can pay online now: [(key, label, amount)]."""
    balance = invoice.balance
    if balance <= 0:
        return []
    booking = invoice.booking
    options = []
    if booking and booking.awaiting_payment:
        deposit_left = booking.prepayment_amount - invoice.amount_paid
        if Decimal("0") < deposit_left < balance:
            options.append(("deposit", "Pay deposit", deposit_left))
    options.append(("full", "Pay full amount", balance))
    return options


def start_attempt(invoice: Invoice, gateway_code: str, option: str = "full") -> OnlinePayment:
    invoice.refresh_from_db()
    if invoice.status == Invoice.Status.VOID:
        raise ValidationError("This invoice is no longer payable.")
    booking = invoice.booking
    if booking and booking.status == booking.Status.AWAITING_PAYMENT and booking.hold_expired:
        raise ValidationError("Your slot hold expired. Please book again.")
    options = {key: amount for key, _, amount in amount_options(invoice)}
    if not options:
        raise ValidationError("Nothing left to pay on this invoice.")
    amount = options.get(option) or options["full"]
    return OnlinePayment.objects.create(
        reference="T" + timezone.localtime().strftime("%y%m%d%H%M%S") + secrets.token_hex(3).upper(),
        invoice=invoice, gateway=gateway_code, amount=amount,
    )


@transaction.atomic
def apply_result(result: GatewayResult, method: str) -> OnlinePayment:
    """Idempotent: a callback delivered twice (return URL + IPN, browser refresh) is applied once."""
    attempt = OnlinePayment.objects.select_for_update().select_related("invoice").filter(
        reference=result.reference
    ).first()
    if not attempt:
        raise ValidationError("Unknown payment reference.")
    if attempt.state != OnlinePayment.State.STARTED:
        return attempt
    attempt.response_code, attempt.response_message = result.code, result.message[:255]
    attempt.raw_response, attempt.completed_at = result.raw, timezone.now()
    if result.amount is not None and result.amount != attempt.amount:
        attempt.state = OnlinePayment.State.FAILED
        attempt.response_message = f"Amount mismatch: expected {attempt.amount}, got {result.amount}"
    elif result.success:
        attempt.state = OnlinePayment.State.SUCCEEDED
        attempt.payment = record_payment(
            attempt.invoice, amount=attempt.amount, method=method, reference=attempt.reference,
            note=f"{attempt.gateway} online payment", allow_overpay=True,
        )
    else:
        attempt.state = OnlinePayment.State.FAILED
    attempt.save()
    return attempt
