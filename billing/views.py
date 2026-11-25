import json
import logging

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from core.models import SiteSettings

from . import payments
from .gateways import GatewayError, get_gateway, jazzcash_hash
from .models import Invoice, OnlinePayment

logger = logging.getLogger(__name__)


def public_invoice(request, token):
    """Shareable, printable invoice (link sent by email / WhatsApp)."""
    invoice = get_object_or_404(Invoice.objects.select_related("booking__court"), token=token)
    club = SiteSettings.load()
    return render(request, "billing/invoice.html", {
        "invoice": invoice, "items": invoice.items.all(), "payments": invoice.payments.all(),
        "can_pay_online": bool(get_gateway(club)) and invoice.balance > 0,
    })


def pay(request, token):
    """Pay an invoice online. Also the landing page right after an online booking that needs a deposit."""
    invoice = get_object_or_404(Invoice.objects.select_related("booking__court"), token=token)
    club = SiteSettings.load()
    gateway = get_gateway(club)
    booking = invoice.booking
    if request.method == "POST":
        if not gateway:
            messages.error(request, "Online payment isn't available right now. Please pay at the club.")
            return redirect("billing:pay", token=token)
        try:
            attempt = payments.start_attempt(invoice, gateway.code, request.POST.get("option", "full"))
        except ValidationError as exc:
            messages.error(request, exc.messages[0])
            return redirect("billing:pay", token=token)
        return gateway.checkout(request, attempt)
    return render(request, "billing/pay.html", {
        "invoice": invoice, "booking": booking, "gateway": gateway,
        "options": payments.amount_options(invoice),
        "failed": invoice.online_payments.filter(state=OnlinePayment.State.FAILED).first()
        if request.GET.get("failed") else None,
    })


def _destination(attempt):
    booking = attempt.invoice.booking
    if booking:
        return reverse("bookings:detail", args=[booking.reference])
    return reverse("billing:public_invoice", args=[attempt.invoice.token])


@csrf_exempt  # the gateway POSTs here from its own domain; trust comes from the signature check
def gateway_return(request, code):
    club = SiteSettings.load()
    gateway = get_gateway(club, code)
    if not gateway:
        messages.error(request, "Payment method not available.")
        return redirect("core:home")
    data = request.POST if request.method == "POST" else request.GET
    try:
        result = gateway.parse_callback(data)
        attempt = payments.apply_result(result, gateway.payment_method)
    except (GatewayError, ValidationError) as exc:
        logger.warning("Rejected %s callback: %s", code, exc)
        messages.error(request, "We couldn't verify that payment. If money was deducted, contact the club "
                                "with your transaction ID.")
        return redirect("core:home")
    if attempt.state == OnlinePayment.State.SUCCEEDED:
        booking = attempt.invoice.booking
        if booking:
            booking.refresh_from_db()
        if booking and booking.status == booking.Status.EXPIRED:
            messages.warning(request, "Payment received, but your slot was taken after the hold expired. "
                                      "The club will refund you or help you rebook.")
        else:
            messages.success(request, "Payment received — thank you!")
        return redirect(_destination(attempt))
    messages.error(request, f"Payment was not completed. {attempt.response_message}".strip())
    return redirect(reverse("billing:pay", args=[attempt.invoice.token]) + "?failed=1")


@csrf_exempt
@require_POST
def jazzcash_ipn(request):
    """Server-to-server notification from JazzCash (configure this URL as the IPN URL in the
    merchant portal). Applies the result even if the customer closed the browser."""
    club = SiteSettings.load()
    gateway = get_gateway(club, "jazzcash")
    if not gateway:
        return JsonResponse({"pp_ResponseCode": "124", "pp_ResponseMessage": "Not configured"}, status=503)
    try:
        data = json.loads(request.body) if request.content_type == "application/json" else request.POST
        payments.apply_result(gateway.parse_callback(data), gateway.payment_method)
        reply = {"pp_ResponseCode": "000", "pp_ResponseMessage": "IPN received"}
    except (GatewayError, ValidationError, ValueError) as exc:
        logger.warning("Rejected JazzCash IPN: %s", exc)
        reply = {"pp_ResponseCode": "124", "pp_ResponseMessage": "Rejected"}
    reply["pp_SecureHash"] = jazzcash_hash(reply, club.jazzcash_integrity_salt)
    return JsonResponse(reply)
