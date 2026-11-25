"""Booking business logic: opening hours, slot grid, pricing, safe creation, cancellation.

Times are stored as timezone-aware datetimes. A "business date" is the club day a slot
belongs to, so a 1:00 AM game after a late night session still sits on the previous day
when the club closes after midnight.
"""
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import ROUND_UP, Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from core.models import SiteSettings
from courts.models import Court

from .models import Booking, BookingExtra, PromoCode
from .notifications import notify_on_commit

CENT = Decimal("0.01")


# --------------------------------------------------------------------------- time helpers
def _aware(d: date, t: time) -> datetime:
    return timezone.make_aware(datetime.combine(d, t), timezone.get_current_timezone())


def opening_window(business_date: date, s: SiteSettings):
    open_dt = _aware(business_date, s.open_time)
    close_day = business_date + timedelta(days=1) if s.closes_after_midnight else business_date
    return open_dt, _aware(close_day, s.close_time)


def combine(business_date: date, t: time, s: SiteSettings) -> datetime:
    """Business date + wall-clock time -> aware datetime (after-midnight aware)."""
    day = business_date
    if s.closes_after_midnight and t < s.open_time:
        day = business_date + timedelta(days=1)
    return _aware(day, t)


def current_business_date(s: SiteSettings, now=None) -> date:
    local = timezone.localtime(now or timezone.now())
    if s.closes_after_midnight and local.time() < s.close_time:
        return local.date() - timedelta(days=1)
    return local.date()


def _in_window(t: time, start: time, end: time) -> bool:
    if start == end:
        return False
    if start < end:
        return start <= t < end
    return t >= start or t < end  # window crosses midnight


def is_peak(slot_start: datetime, business_date: date, s: SiteSettings) -> bool:
    if s.weekend_all_day_peak and business_date.weekday() >= 5:
        return True
    return _in_window(timezone.localtime(slot_start).time(), s.peak_start, s.peak_end)


# --------------------------------------------------------------------------- pricing
def slot_price(court: Court, peak: bool, s: SiteSettings) -> Decimal:
    return (court.hourly_rate(peak) * Decimal(s.slot_minutes) / Decimal(60)).quantize(CENT)


def quote_court(court: Court, business_date: date, start_at: datetime, duration: int, s: SiteSettings) -> Decimal:
    total, t = Decimal("0"), start_at
    step = timedelta(minutes=s.slot_minutes)
    for _ in range(duration // s.slot_minutes):
        total += slot_price(court, is_peak(t, business_date, s), s)
        t += step
    return total


# --------------------------------------------------------------------------- slot grid
@dataclass
class Slot:
    start: datetime
    end: datetime
    status: str  # "available" | "booked" | "past"
    price: Decimal
    peak: bool
    booking: Booking | None = None

    @property
    def value(self):
        return timezone.localtime(self.start).strftime("%H:%M")

    @property
    def local_start(self):
        return timezone.localtime(self.start)

    @property
    def available(self):
        return self.status == "available"


def day_slots(court: Court, business_date: date, s: SiteSettings | None = None, now=None, bookings=None):
    s = s or SiteSettings.load()
    now = now or timezone.now()
    open_dt, close_dt = opening_window(business_date, s)
    if bookings is None:
        bookings = list(Booking.objects.active().filter(court=court, start_at__lt=close_dt, end_at__gt=open_dt))
    cutoff = now + timedelta(minutes=s.min_notice_minutes)
    step = timedelta(minutes=s.slot_minutes)
    slots, t = [], open_dt
    while t + step <= close_dt:
        end = t + step
        booking = next((b for b in bookings if b.court_id == court.id and b.start_at < end and b.end_at > t), None)
        peak = is_peak(t, business_date, s)
        status = "booked" if booking else ("past" if t < cutoff else "available")
        slots.append(Slot(t, end, status, slot_price(court, peak, s), peak, booking))
        t = end
    return slots


def bookable_dates(s: SiteSettings, now=None):
    first = current_business_date(s, now)
    return [first + timedelta(days=i) for i in range(s.booking_window_days + 1)]


# --------------------------------------------------------------------------- validation & creation
def validate_slot(court, business_date, start_at, duration, s, *, staff=False, now=None):
    now = now or timezone.now()
    if not court.is_active:
        raise ValidationError("This court is not available for booking right now.")
    if duration <= 0 or duration % s.slot_minutes:
        raise ValidationError(f"Duration must be a multiple of {s.slot_minutes} minutes.")
    if not staff and duration not in s.duration_list:
        raise ValidationError("Please choose one of the available durations.")
    open_dt, close_dt = opening_window(business_date, s)
    end_at = start_at + timedelta(minutes=duration)
    if start_at < open_dt or end_at > close_dt:
        raise ValidationError("That time is outside our opening hours.")
    if (start_at - open_dt) % timedelta(minutes=s.slot_minutes):
        raise ValidationError("Please pick a start time from the schedule.")
    if not staff:
        if start_at < now + timedelta(minutes=s.min_notice_minutes):
            raise ValidationError("That slot has already started or is too soon to book online.")
        if business_date > current_business_date(s, now) + timedelta(days=s.booking_window_days):
            raise ValidationError(f"Bookings open {s.booking_window_days} days in advance.")
    if Booking.objects.overlapping(court, start_at, end_at).exists():
        raise ValidationError("Sorry, that slot was just booked. Please choose another time.")
    return end_at


def resolve_promo(code: str, amount: Decimal, business_date: date, lock=False):
    """Return (promo, discount). Raises ValidationError for an unusable code."""
    code = (code or "").strip().upper()
    if not code:
        return None, Decimal("0")
    qs = PromoCode.objects.select_for_update() if lock else PromoCode.objects
    promo = qs.filter(code=code).first()
    if not promo:
        raise ValidationError("That promo code doesn't exist.")
    problem = promo.problem(amount, business_date)
    if problem:
        raise ValidationError(problem)
    return promo, promo.discount_for(amount)


@transaction.atomic
def create_booking(
    *,
    court: Court,
    business_date: date,
    start_at: datetime,
    duration: int,
    customer_name: str,
    customer_phone: str,
    customer_email: str = "",
    customer=None,
    extras=None,  # iterable of (Extra, quantity)
    promo_code: str = "",
    source: str = Booking.Source.ONLINE,
    created_by=None,
    staff: bool = False,
    notes: str = "",
    players: int = 4,
    series=None,
    notify: bool = True,
) -> Booking:
    from billing.services import create_invoice_for_booking

    s = SiteSettings.load()
    # Lock the court row so two people can't grab the same slot at the same moment.
    court = Court.objects.select_for_update().get(pk=court.pk)
    end_at = validate_slot(court, business_date, start_at, duration, s, staff=staff)

    extras = [(e, int(q)) for e, q in (extras or []) if int(q) > 0]
    court_amount = quote_court(court, business_date, start_at, duration, s)
    extras_amount = sum((e.price * q for e, q in extras), Decimal("0"))
    promo, discount = resolve_promo(promo_code, court_amount + extras_amount, business_date, lock=True)

    needs_prepayment = not staff and s.prepayment_active
    if needs_prepayment:
        status = Booking.Status.AWAITING_PAYMENT
    elif staff or s.auto_confirm:
        status = Booking.Status.CONFIRMED
    else:
        status = Booking.Status.PENDING

    booking = Booking.objects.create(
        court=court,
        customer=customer,
        customer_name=customer_name.strip(),
        customer_phone=customer_phone.strip(),
        customer_email=(customer_email or "").strip().lower(),
        business_date=business_date,
        start_at=start_at,
        end_at=end_at,
        duration_minutes=duration,
        court_amount=court_amount,
        extras_amount=extras_amount,
        discount_amount=discount,
        promo_code=promo,
        status=status,
        source=source,
        notes=notes,
        players=players,
        created_by=created_by,
        series=series,
    )
    BookingExtra.objects.bulk_create(
        [BookingExtra(booking=booking, extra=e, quantity=q, unit_price=e.price) for e, q in extras]
    )
    if promo:
        PromoCode.objects.filter(pk=promo.pk).update(times_used=F("times_used") + 1)

    invoice = create_invoice_for_booking(booking, s)
    if needs_prepayment:
        booking.prepayment_amount = prepayment_for(invoice.total, s)
        if booking.prepayment_amount > 0:
            booking.payment_due_at = timezone.now() + timedelta(minutes=s.payment_hold_minutes)
        else:  # e.g. 100% promo: nothing to pay
            booking.status = Booking.Status.CONFIRMED if s.auto_confirm else Booking.Status.PENDING
        booking.save(update_fields=["prepayment_amount", "payment_due_at", "status", "updated_at"])
    if notify and booking.status in (Booking.Status.CONFIRMED, Booking.Status.PENDING) \
            and source != Booking.Source.WALK_IN:
        notify_on_commit(booking, "confirmation")
    return booking


def prepayment_for(total: Decimal, s: SiteSettings) -> Decimal:
    """Amount to pay online to confirm, rounded up to a whole rupee."""
    if s.prepayment_mode == "full":
        return total
    if s.prepayment_mode == "deposit":
        amount = (total * Decimal(s.deposit_percent) / Decimal(100)).quantize(Decimal("1"), rounding=ROUND_UP)
        return min(amount, total)
    return Decimal("0")


@transaction.atomic
def confirm_after_payment(booking: Booking) -> bool:
    """Called once a payment lands. Confirms an awaiting booking whose online payment covers the
    required amount. If the hold had already lapsed and someone else took the slot, it returns
    False and the payment stays on the invoice for the owner to refund (shown on the dashboard)."""
    from billing.models import Invoice

    s = SiteSettings.load()
    booking = Booking.objects.select_for_update().get(pk=booking.pk)
    if booking.status not in (Booking.Status.AWAITING_PAYMENT, Booking.Status.EXPIRED):
        return booking.is_active
    invoice = Invoice.objects.get(booking=booking)
    if invoice.amount_paid < booking.prepayment_amount:
        return False
    Court.objects.select_for_update().get(pk=booking.court_id)
    clash = Booking.objects.overlapping(booking.court, booking.start_at, booking.end_at).exclude(pk=booking.pk)
    if clash.exists():
        booking.status = Booking.Status.EXPIRED
        booking.notes = (booking.notes + "\nPaid after the hold expired and the slot was re-booked — refund due.").strip()
        booking.save(update_fields=["status", "notes", "updated_at"])
        return False
    if invoice.status == Invoice.Status.VOID:  # was voided when the hold expired
        invoice.status = Invoice.Status.UNPAID
        invoice.recalculate()
    booking.status = Booking.Status.CONFIRMED if s.auto_confirm else Booking.Status.PENDING
    booking.payment_due_at = None
    booking.save(update_fields=["status", "payment_due_at", "updated_at"])
    notify_on_commit(booking, "confirmation")
    return True


def expire_unpaid_bookings(now=None) -> int:
    """Mark lapsed payment holds as expired, void their unpaid invoices and give back promo uses.
    Slots are already free the moment a hold lapses; this tidies up the records."""
    from billing.services import void_invoice_if_unpaid

    now = now or timezone.now()
    count = 0
    for pk in Booking.objects.filter(status=Booking.Status.AWAITING_PAYMENT, payment_due_at__lt=now).values_list(
        "pk", flat=True
    ):
        with transaction.atomic():
            booking = Booking.objects.select_for_update().get(pk=pk)
            if booking.status != Booking.Status.AWAITING_PAYMENT or not booking.payment_due_at < now:
                continue
            booking.status = Booking.Status.EXPIRED
            booking.cancel_reason = "Payment not completed in time"
            booking.save(update_fields=["status", "cancel_reason", "updated_at"])
            if booking.promo_code_id:
                PromoCode.objects.filter(pk=booking.promo_code_id, times_used__gt=0).update(
                    times_used=F("times_used") - 1
                )
            void_invoice_if_unpaid(booking)
            count += 1
    return count


ALLOWED_TRANSITIONS = {
    # The club can confirm an awaiting booking by hand, e.g. when the customer paid cash instead.
    Booking.Status.AWAITING_PAYMENT: {Booking.Status.CONFIRMED, Booking.Status.CANCELLED},
    Booking.Status.EXPIRED: set(),
    Booking.Status.PENDING: {Booking.Status.CONFIRMED, Booking.Status.CANCELLED},
    Booking.Status.CONFIRMED: {
        Booking.Status.CHECKED_IN,
        Booking.Status.COMPLETED,
        Booking.Status.NO_SHOW,
        Booking.Status.CANCELLED,
    },
    Booking.Status.CHECKED_IN: {Booking.Status.COMPLETED},
    Booking.Status.COMPLETED: set(),
    Booking.Status.CANCELLED: set(),
    Booking.Status.NO_SHOW: set(),
}


@transaction.atomic
def cancel_booking(booking: Booking, *, reason="", staff=False, now=None) -> Booking:
    from billing.services import void_invoice_if_unpaid

    s = SiteSettings.load()
    booking = Booking.objects.select_for_update().get(pk=booking.pk)
    if not staff and not booking.customer_can_cancel(s.cancellation_hours, now):
        raise ValidationError(
            f"Online cancellation closes {s.cancellation_hours} hours before your game. Please contact the club."
        )
    if Booking.Status.CANCELLED not in ALLOWED_TRANSITIONS[booking.status]:
        raise ValidationError("This booking can't be cancelled.")
    booking.status = Booking.Status.CANCELLED
    booking.cancelled_at = timezone.now()
    booking.cancel_reason = reason[:200]
    booking.save(update_fields=["status", "cancelled_at", "cancel_reason", "updated_at"])
    if booking.promo_code_id:
        PromoCode.objects.filter(pk=booking.promo_code_id, times_used__gt=0).update(
            times_used=F("times_used") - 1
        )
    void_invoice_if_unpaid(booking)
    return booking


@transaction.atomic
def change_status(booking: Booking, new_status: str) -> Booking:
    if new_status == Booking.Status.CANCELLED:
        return cancel_booking(booking, staff=True, reason="Cancelled by club")
    booking = Booking.objects.select_for_update().get(pk=booking.pk)
    if new_status not in ALLOWED_TRANSITIONS.get(booking.status, set()):
        raise ValidationError(
            f"Can't change a {booking.get_status_display().lower()} booking to "
            f"{Booking.Status(new_status).label.lower()}."
        )
    booking.status = new_status
    booking.save(update_fields=["status", "updated_at"])
    return booking


# --------------------------------------------------------------------------- weekly series
def create_series(*, court, first_date, start_time, duration, weeks, customer_name, customer_phone,
                  customer_email="", source=Booking.Source.PHONE, created_by=None, notes="", players=4, extras=None):
    """Book the same slot every week. Weeks that clash with an existing booking are skipped and
    reported, the rest are booked (each with its own invoice, payable week by week or from credit)."""
    from .models import BookingSeries

    s = SiteSettings.load()
    if not 2 <= weeks <= 52:
        raise ValidationError("Choose between 2 and 52 weeks.")
    series = BookingSeries.objects.create(
        court=court, customer_name=customer_name, customer_phone=customer_phone, weekday=first_date.weekday(),
        start_time=start_time, duration_minutes=duration, first_date=first_date, weeks=weeks, created_by=created_by,
    )
    created, skipped = [], []
    for week in range(weeks):
        day = first_date + timedelta(weeks=week)
        try:
            created.append(create_booking(
                court=court, business_date=day, start_at=combine(day, start_time, s), duration=duration,
                customer_name=customer_name, customer_phone=customer_phone, customer_email=customer_email,
                extras=extras, source=source, created_by=created_by, staff=True, notes=notes, players=players,
                series=series, notify=(week == 0),
            ))
        except ValidationError as exc:
            skipped.append((day, exc.messages[0]))
    if not created:
        series.delete()
        series = None
    return series, created, skipped


def cancel_series_remaining(series, now=None) -> int:
    now = now or timezone.now()
    count = 0
    for booking in series.bookings.active().filter(start_at__gt=now):
        cancel_booking(booking, staff=True, reason="Weekly booking cancelled")
        count += 1
    return count
