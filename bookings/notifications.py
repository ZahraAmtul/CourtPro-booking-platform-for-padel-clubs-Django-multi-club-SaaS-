"""Customer messages: booking confirmations and game reminders.

Two channels:
* WhatsApp Cloud API templates — automatic, from the club's own number (when connected)
* Email — automatic whenever the customer gave an email

Every attempt is logged in `Notification`, which makes sending idempotent (a message is never
sent twice) and lets the owner see what went out. Failed sends are retried by the scheduler.

Click-to-chat links (`whatsapp_link`) stay available in the dashboard as a free manual option.
"""
import logging
import re
from datetime import timedelta
from urllib.parse import quote

from django.conf import settings
from django.core.mail import send_mail
from django.db import transaction
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone

from .whatsapp import WhatsAppError, send_template

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- helpers
def normalize_phone(phone: str, country_code: str = "92") -> str:
    """'0300-1234567' -> '923001234567'. Leaves international numbers as digits."""
    digits = re.sub(r"\D", "", phone or "")
    if digits.startswith("00"):
        digits = digits[2:]
    elif digits.startswith("0"):
        digits = country_code + digits[1:]
    return digits


def phones_match(a: str, b: str) -> bool:
    da, db = re.sub(r"\D", "", a or ""), re.sub(r"\D", "", b or "")
    return bool(da) and bool(db) and da[-10:] == db[-10:]


def invoice_url(booking) -> str:
    invoice = getattr(booking, "invoice", None)
    if not invoice:
        return ""
    return booking.club.base_url + reverse("billing:public_invoice", args=[invoice.token])


def pay_url(booking) -> str:
    invoice = getattr(booking, "invoice", None)
    if not invoice:
        return ""
    return booking.club.base_url + reverse("billing:pay", args=[invoice.token])


def booking_url(booking) -> str:
    return booking.club.base_url + reverse("bookings:detail", args=[booking.reference])


def when_text(booking) -> str:
    start, end = timezone.localtime(booking.start_at), timezone.localtime(booking.end_at)
    return f"{start:%a %d %b}, {start:%I:%M %p} - {end:%I:%M %p}"


def booking_message(booking, club) -> str:
    """Pre-filled text for the free click-to-chat button in the dashboard."""
    invoice = getattr(booking, "invoice", None)
    lines = [
        f"Hi {booking.customer_name.split(' ')[0]}! Your padel booking at {club.club_name} is "
        f"{booking.get_status_display().lower()}.",
        "",
        f"Ref: {booking.reference}",
        f"Court: {booking.court.name}",
        f"When: {when_text(booking)}",
    ]
    if invoice:
        lines.append(f"Total: {club.currency_symbol} {invoice.total:,.0f}")
        if invoice.balance > 0 and club.gateway_ready:
            lines.append(f"Pay online: {pay_url(booking)}")
        else:
            lines.append(f"Invoice: {invoice_url(booking)}")
    if club.address:
        lines += ["", f"Location: {club.address}"]
    return "\n".join(lines)


def whatsapp_link(booking, club) -> str:
    return f"https://wa.me/{normalize_phone(booking.customer_phone)}?text={quote(booking_message(booking, club))}"


# --------------------------------------------------------------------------- templates
def template_params(kind: str, booking, club) -> tuple[str, list[str]]:
    """Template name + body parameters. Keep in sync with the template text in README."""
    first_name = booking.customer_name.split(" ")[0]
    if kind == "confirmation":
        # Hi {{1}}, your padel court at {{2}} is booked. Ref: {{3}} | Court: {{4}} | When: {{5}}. Details: {{6}}
        return club.whatsapp_confirmation_template, [
            first_name, club.club_name, booking.reference, booking.court.name, when_text(booking), booking_url(booking),
        ]
    # Reminder: Hi {{1}}, reminder: you're playing at {{2}} {{3}} on {{4}}. Ref {{5}}. Manage: {{6}}
    when = "tomorrow" if kind == "reminder_24h" else "soon"
    return club.whatsapp_reminder_template, [
        first_name, club.club_name, when, f"{booking.court.name}, {when_text(booking)}", booking.reference,
        booking_url(booking),
    ]


def _send_email(kind, booking, club):
    context = {
        "booking": booking, "club": club, "kind": kind,
        "invoice_url": invoice_url(booking), "booking_url": booking_url(booking),
    }
    if kind == "confirmation":
        subject = f"{club.club_name}: booking {booking.get_status_display().lower()} ({booking.reference})"
        text = render_to_string("emails/booking_confirmation.txt", context)
        html = render_to_string("emails/booking_confirmation.html", context)
    else:
        subject = f"Reminder: padel at {club.club_name} — {when_text(booking)}"
        text = render_to_string("emails/booking_reminder.txt", context)
        html = None
    send_mail(subject, text, None, [booking.customer_email], html_message=html)
    return ""


def _send_whatsapp(kind, booking, club):
    template, params = template_params(kind, booking, club)
    return send_template(club, normalize_phone(booking.customer_phone), template, params)


# --------------------------------------------------------------------------- sending
def channels_for(booking, club):
    channels = []
    if club.whatsapp_ready and club.club.has_whatsapp and booking.customer_phone:
        channels.append("whatsapp")
    if booking.customer_email:
        channels.append("email")
    return channels


def send_notification(booking_id: int, kind: str) -> dict:
    """Send `kind` on every available channel, skipping channels already sent. Returns {channel: state}."""
    from core.models import SiteSettings

    from .models import Booking, Notification

    from tenants.context import use_club

    booking = Booking.all_clubs.select_related("court", "invoice", "club").filter(pk=booking_id).first()
    if not booking or not booking.is_active:
        return {}
    with use_club(booking.club):
        return _send_for_booking(booking, kind)


def _send_for_booking(booking, kind):
    from core.models import SiteSettings

    from .models import Notification

    club = SiteSettings.load(booking.club)
    if kind == "confirmation" and booking.awaiting_payment:
        return {}  # confirmation goes out once paid
    results = {}
    for channel in channels_for(booking, club):
        with transaction.atomic():
            note, _ = Notification.objects.select_for_update().get_or_create(
                booking=booking, kind=kind, channel=channel
            )
            if note.state == Notification.State.SENT or note.attempts >= Notification.MAX_ATTEMPTS:
                results[channel] = note.state
                continue
            note.attempts += 1
            try:
                sender = _send_whatsapp if channel == "whatsapp" else _send_email
                note.provider_id = sender(kind, booking, club) or ""
                note.state, note.error = Notification.State.SENT, ""
            except WhatsAppError as exc:
                note.state, note.error = Notification.State.FAILED, str(exc)[:300]
                logger.warning("WhatsApp %s for %s failed: %s", kind, booking.reference, exc)
            except Exception as exc:  # email/SMTP errors must never break a booking
                note.state, note.error = Notification.State.FAILED, str(exc)[:300]
                logger.exception("%s %s for %s failed", channel, kind, booking.reference)
            note.save()
            results[channel] = note.state
    return results


def notify_on_commit(booking, kind: str) -> None:
    pk = booking.pk
    transaction.on_commit(lambda: send_notification(pk, kind))


def due_reminders(club, now=None):
    """Yield (booking, kind) pairs that should get a reminder right now.

    * 24h reminder: game starts in 3-24 hours, booked at least 12 hours before start
    * 2h reminder:  game starts within 2 hours (and not in the next 10 minutes), booked 3+ hours before start
    Bookings made at the last minute don't need a reminder — they just got a confirmation.
    """
    from .models import Booking

    now = now or timezone.now()
    reminding = Booking.objects.active(now).filter(
        status__in=[Booking.Status.CONFIRMED, Booking.Status.PENDING]
    ).select_related("court", "invoice")
    if club.remind_24h:
        qs = reminding.filter(start_at__gt=now + timedelta(hours=3), start_at__lte=now + timedelta(hours=24))
        for booking in qs:
            if booking.created_at <= booking.start_at - timedelta(hours=12):
                yield booking, "reminder_24h"
    if club.remind_2h:
        qs = reminding.filter(start_at__gt=now + timedelta(minutes=10), start_at__lte=now + timedelta(hours=2))
        for booking in qs:
            if booking.created_at <= booking.start_at - timedelta(hours=3):
                yield booking, "reminder_2h"


def send_due_reminders(now=None) -> int:
    from core.models import SiteSettings

    from .models import Notification

    club = SiteSettings.load()
    sent = 0
    for booking, kind in due_reminders(club, now):
        already = set(
            Notification.objects.filter(booking=booking, kind=kind)
            .filter(state=Notification.State.SENT).values_list("channel", flat=True)
        )
        if set(channels_for(booking, club)) <= already:
            continue
        results = send_notification(booking.pk, kind)
        sent += sum(1 for ch, state in results.items() if state == "sent" and ch not in already)
    return sent


def retry_failed(now=None) -> int:
    """Retry confirmations that failed (e.g. WhatsApp briefly down) for upcoming games."""
    from .models import Notification

    now = now or timezone.now()
    pending = (
        Notification.objects.filter(
            state=Notification.State.FAILED, attempts__lt=Notification.MAX_ATTEMPTS,
            kind=Notification.Kind.CONFIRMATION, booking__start_at__gt=now,
        ).values_list("booking_id", flat=True).distinct()
    )
    return sum(1 for pk in pending if send_notification(pk, "confirmation"))
