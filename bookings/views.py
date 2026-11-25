from datetime import date, datetime

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from core.models import SiteSettings
from courts.models import Court

from . import services
from .forms import BookingDetailsForm, ManageBookingForm
from .models import Booking
from .notifications import phones_match

SESSION_KEY = "my_booking_refs"


def _remember(request, reference):
    refs = request.session.get(SESSION_KEY, [])
    if reference not in refs:
        refs.append(reference)
        request.session[SESSION_KEY] = refs[-20:]


def _can_view(request, booking):
    user = request.user
    if user.is_authenticated and (getattr(request, "club_role", None) or booking.customer_id == user.id):
        return True
    return booking.reference in request.session.get(SESSION_KEY, [])


def _parse_date(value, default):
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return default


def _paused(request):
    return render(request, "bookings/paused.html", status=503)


def book(request):
    """Step 1: pick a date, court, start time and duration."""
    if not request.club.is_operational:
        return _paused(request)
    s = SiteSettings.load()
    dates = services.bookable_dates(s)
    selected = _parse_date(request.GET.get("date"), dates[0])
    if selected not in dates:
        selected = dates[0]

    courts = list(Court.objects.filter(is_active=True))
    court_filter = request.GET.get("court")
    if court_filter:
        courts = [c for c in courts if c.slug == court_filter] or courts

    open_dt, close_dt = services.opening_window(selected, s)
    bookings = list(Booking.objects.active().filter(court__in=courts, start_at__lt=close_dt, end_at__gt=open_dt))
    grid = [
        {"court": court, "slots": services.day_slots(court, selected, s, bookings=bookings)} for court in courts
    ]
    for row in grid:
        row["free"] = sum(1 for slot in row["slots"] if slot.available)

    return render(
        request,
        "bookings/book.html",
        {
            "dates": dates,
            "selected_date": selected,
            "grid": grid,
            "durations": s.duration_list,
            "court_filter": court_filter,
        },
    )


def details(request):
    """Step 2: customer details, extras and promo code, then create the booking."""
    if not request.club.is_operational:
        return _paused(request)
    s = SiteSettings.load()
    params = request.POST if request.method == "POST" else request.GET
    court = Court.objects.filter(slug=params.get("court"), is_active=True).first()
    business_date = _parse_date(params.get("date"), None)
    try:
        start_time = datetime.strptime(params.get("start", ""), "%H:%M").time()
        duration = int(params.get("duration", 0))
    except ValueError:
        start_time, duration = None, 0
    if not (court and business_date and start_time and duration):
        messages.error(request, "Please choose a court and time first.")
        return redirect("bookings:book")

    start_at = services.combine(business_date, start_time, s)
    back_url = reverse("bookings:book") + f"?date={business_date.isoformat()}"
    try:
        end_at = services.validate_slot(court, business_date, start_at, duration, s)
    except ValidationError as exc:
        messages.error(request, exc.messages[0])
        return redirect(back_url)

    initial = {}
    if request.user.is_authenticated:
        initial = {
            "customer_name": request.user.get_full_name(),
            "customer_phone": request.user.phone,
            "customer_email": request.user.email,
        }
    form = BookingDetailsForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        try:
            booking = services.create_booking(
                court=court,
                business_date=business_date,
                start_at=start_at,
                duration=duration,
                customer_name=form.cleaned_data["customer_name"],
                customer_phone=form.cleaned_data["customer_phone"],
                customer_email=form.cleaned_data["customer_email"],
                customer=request.user if request.user.is_authenticated else None,
                extras=form.selected_extras(),
                promo_code=form.cleaned_data["promo_code"],
                players=form.cleaned_data["players"],
                notes=form.cleaned_data["notes"],
            )
        except ValidationError as exc:
            msg = exc.messages[0]
            if "promo" in msg.lower():
                form.add_error("promo_code", msg)
            else:
                messages.error(request, msg)
                return redirect(back_url)
        else:
            _remember(request, booking.reference)
            if booking.status == Booking.Status.AWAITING_PAYMENT:
                return redirect("billing:pay", token=booking.invoice.token)
            return redirect("bookings:detail", reference=booking.reference)

    court_amount = services.quote_court(court, business_date, start_at, duration, s)
    return render(
        request,
        "bookings/details.html",
        {
            "form": form,
            "court": court,
            "business_date": business_date,
            "start_at": start_at,
            "end_at": end_at,
            "duration": duration,
            "court_amount": court_amount,
            "start_value": start_time.strftime("%H:%M"),
            "prepay_mode": s.prepayment_mode if s.prepayment_active else "none",
            "back_url": back_url,
        },
    )


def detail(request, reference):
    booking = get_object_or_404(Booking.objects.select_related("court", "invoice"), reference=reference)
    if not _can_view(request, booking):
        messages.info(request, "Enter your booking reference and phone number to view this booking.")
        return redirect(reverse("bookings:manage") + f"?reference={reference}")
    s = SiteSettings.load()
    return render(
        request,
        "bookings/detail.html",
        {
            "booking": booking,
            "can_cancel": booking.customer_can_cancel(s.cancellation_hours),
            "extras": booking.extras.select_related("extra"),
        },
    )


@require_POST
def cancel(request, reference):
    booking = get_object_or_404(Booking, reference=reference)
    if not _can_view(request, booking):
        return redirect("bookings:manage")
    try:
        services.cancel_booking(booking, reason="Cancelled by customer online")
        messages.success(request, "Your booking has been cancelled.")
    except ValidationError as exc:
        messages.error(request, exc.messages[0])
    return redirect("bookings:detail", reference=reference)


def manage(request):
    form = ManageBookingForm(request.POST or None, initial={"reference": request.GET.get("reference", "")})
    if request.method == "POST" and form.is_valid():
        booking = Booking.objects.filter(reference=form.cleaned_data["reference"]).first()
        if booking and phones_match(booking.customer_phone, form.cleaned_data["phone"]):
            _remember(request, booking.reference)
            if booking.status == Booking.Status.AWAITING_PAYMENT:
                return redirect("billing:pay", token=booking.invoice.token)
            return redirect("bookings:detail", reference=booking.reference)
        form.add_error(None, "We couldn't find a booking with that reference and phone number.")
    return render(request, "bookings/manage.html", {"form": form})
