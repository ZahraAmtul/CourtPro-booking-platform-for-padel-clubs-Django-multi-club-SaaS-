import csv
from datetime import date, timedelta
from decimal import Decimal

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db.models import Count, Max, Q, Sum
from django.db.models.functions import ExtractHour, TruncDate
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from billing import services as billing
from billing import reports, wallets
from billing.models import CreditPackage, DayClose, Invoice, Payment, Wallet
from bookings import services
from bookings.forms import StaffBookingForm
from bookings.models import Booking, Extra, PromoCode
from bookings.notifications import normalize_phone, pay_url, whatsapp_link
from bookings.whatsapp import WhatsAppError, send_template
from core.models import SiteSettings
from courts.models import Court
from tenants.decorators import club_member_required, owner_required, staff_required
from tenants.models import Membership

from .forms import (AdjustCreditForm, CourtForm, CreditPackageForm, DayCloseForm, ExtraForm, InvoiceForm,
                    InvoiceItemFormSet, PaymentForm, PromoForm, SellCreditForm,
                    SettingsForm)

# Staff (reception) can run the day; owners also control prices, settings, exports, voids and the team.


def _back(request, default, **kwargs):
    """Redirect to a safe ?next= URL posted by the form, else to `default`."""
    nxt = request.POST.get("next", "")
    if nxt and url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()}):
        return redirect(nxt)
    return redirect(default, **kwargs)


def _parse_date(value, default):
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------- overview
@staff_required
def overview(request):
    s = SiteSettings.load()
    now = timezone.now()
    today = services.current_business_date(s)
    local_today = timezone.localdate()
    courts = list(Court.objects.filter(is_active=True))

    todays = list(
        Booking.objects.active().filter(business_date=today).select_related("court", "invoice").order_by("start_at")
    )
    open_dt, close_dt = services.opening_window(today, s)
    capacity = (close_dt - open_dt).total_seconds() / 60 * max(len(courts), 1)
    occupancy = round(sum(b.duration_minutes for b in todays) / capacity * 100) if capacity else 0

    payments = Payment.objects.exclude(invoice__status=Invoice.Status.VOID).filter(
        method__in=Payment.CASH_IN_METHODS)  # credit spend was collected when the credit was sold
    revenue_today = payments.filter(paid_at__date=local_today).aggregate(s=Sum("amount"))["s"] or 0
    revenue_month = (
        payments.filter(paid_at__year=local_today.year, paid_at__month=local_today.month)
        .aggregate(s=Sum("amount"))["s"] or 0
    )
    open_invoices = Invoice.objects.filter(status__in=[Invoice.Status.UNPAID, Invoice.Status.PARTIAL])
    agg = open_invoices.aggregate(t=Sum("total"), p=Sum("amount_paid"))
    outstanding = (agg["t"] or 0) - (agg["p"] or 0)

    # Revenue for the last 14 days
    start = local_today - timedelta(days=13)
    by_day = {
        row["day"]: row["total"]
        for row in payments.filter(paid_at__date__gte=start)
        .annotate(day=TruncDate("paid_at")).values("day").annotate(total=Sum("amount"))
    }
    chart = [(start + timedelta(days=i), by_day.get(start + timedelta(days=i), Decimal("0"))) for i in range(14)]
    chart_max = max([v for _, v in chart] + [Decimal("1")])
    chart = [{"day": d, "value": v, "pct": int(v / chart_max * 100)} for d, v in chart]

    # Busiest start hours over the last 30 days — helps decide peak pricing & promos
    hour_counts = dict(
        Booking.objects.active().filter(start_at__gte=now - timedelta(days=30))
        .annotate(h=ExtractHour("start_at")).values_list("h").annotate(c=Count("id")).values_list("h", "c")
    )
    hours, t = [], open_dt
    while t < close_dt:
        hours.append(timezone.localtime(t).hour)
        t += timedelta(hours=1)
    hour_max = max(list(hour_counts.values()) + [1])
    busiest = [{"hour": h, "count": hour_counts.get(h, 0), "pct": int(hour_counts.get(h, 0) / hour_max * 100)}
               for h in hours]

    top_customers = (
        Booking.objects.active().filter(start_at__gte=now - timedelta(days=90))
        .values("customer_phone").annotate(name=Max("customer_name"), visits=Count("id"))
        .order_by("-visits")[:5]
    )

    return render(request, "dashboard/overview.html", {
        "today": today,
        "todays": todays,
        "occupancy": occupancy,
        "revenue_today": revenue_today,
        "revenue_month": revenue_month,
        "outstanding": outstanding,
        "open_invoice_count": open_invoices.count(),
        "pending": Booking.objects.filter(status=Booking.Status.PENDING).select_related("court")[:10],
        "awaiting": Booking.objects.active().filter(status=Booking.Status.AWAITING_PAYMENT)
        .select_related("court").order_by("payment_due_at")[:10],
        "refunds_due": Booking.objects.filter(
            status__in=[Booking.Status.CANCELLED, Booking.Status.EXPIRED], invoice__amount_paid__gt=0
        ).exclude(notes__icontains="[refund handled]").select_related("court", "invoice").order_by("-start_at")[:10],
        "upcoming": Booking.objects.active().filter(start_at__gte=now).select_related("court")
        .order_by("start_at")[:8],
        "chart": chart,
        "busiest": busiest,
        "top_customers": top_customers,
        "unclosed_day": _unclosed_day(today),
        "nav": "overview",
    })


def _unclosed_day(today):
    """Yesterday's business day if it took cash but nobody counted the drawer."""
    yesterday = today - timedelta(days=1)
    if DayClose.objects.filter(business_date=yesterday).exists():
        return None
    return yesterday if reports.day_summary(yesterday)["expected_cash"] > 0 else None


# --------------------------------------------------------------------------- schedule
@staff_required
def schedule(request):
    s = SiteSettings.load()
    day = _parse_date(request.GET.get("date"), services.current_business_date(s))
    courts = list(Court.objects.filter(is_active=True))
    open_dt, close_dt = services.opening_window(day, s)
    bookings = list(
        Booking.objects.active().filter(court__in=courts, start_at__lt=close_dt, end_at__gt=open_dt)
        .select_related("invoice")
    )
    now = timezone.now()
    grid = {c.id: services.day_slots(c, day, s, now=now - timedelta(minutes=s.min_notice_minutes),
                                     bookings=bookings) for c in courts}
    rows = []
    n = len(next(iter(grid.values()))) if grid else 0
    for i in range(n):
        cells = []
        for court in courts:
            slots = grid[court.id]
            slot = slots[i]
            if slot.booking:
                if i == 0 or slots[i - 1].booking is None or slots[i - 1].booking.pk != slot.booking.pk:
                    span = 1
                    while i + span < n and slots[i + span].booking and slots[i + span].booking.pk == slot.booking.pk:
                        span += 1
                    cells.append({"type": "booking", "booking": slot.booking, "rowspan": span})
                else:
                    cells.append({"type": "skip"})
            else:
                cells.append({"type": "free", "slot": slot, "court": court, "past": slot.end <= now})
        first = grid[courts[0].id][i]
        rows.append({"time": first.local_start, "peak": first.peak, "cells": cells})

    return render(request, "dashboard/schedule.html", {
        "day": day,
        "prev_day": day - timedelta(days=1),
        "next_day": day + timedelta(days=1),
        "today": services.current_business_date(s),
        "courts": courts,
        "rows": rows,
        "count": len(bookings),
        "nav": "schedule",
    })


# --------------------------------------------------------------------------- bookings
def _filtered_bookings(request):
    qs = Booking.objects.select_related("court", "invoice")
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(Q(reference__icontains=q) | Q(customer_name__icontains=q) | Q(customer_phone__icontains=q)
                       | Q(customer_email__icontains=q))
    if request.GET.get("status"):
        qs = qs.filter(status=request.GET["status"])
    if request.GET.get("court"):
        qs = qs.filter(court__slug=request.GET["court"])
    if request.GET.get("source"):
        qs = qs.filter(source=request.GET["source"])
    date_from = _parse_date(request.GET.get("from"), None)
    date_to = _parse_date(request.GET.get("to"), None)
    if date_from:
        qs = qs.filter(business_date__gte=date_from)
    if date_to:
        qs = qs.filter(business_date__lte=date_to)
    return qs


@staff_required
def booking_list(request):
    qs = _filtered_bookings(request)
    page = Paginator(qs, 25).get_page(request.GET.get("page"))
    return render(request, "dashboard/booking_list.html", {
        "page": page,
        "statuses": Booking.Status.choices,
        "sources": Booking.Source.choices,
        "courts": Court.objects.all(),
        "nav": "bookings",
    })


@staff_required
def booking_create(request):
    s = SiteSettings.load()
    initial = {
        "business_date": _parse_date(request.GET.get("date"), services.current_business_date(s)),
        "start_time": request.GET.get("time", ""),
        "source": "walk_in",
    }
    court = Court.objects.filter(slug=request.GET.get("court")).first()
    if court:
        initial["court"] = court
    form = StaffBookingForm(request.POST or None, initial=initial, slot_minutes=s.slot_minutes)
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        common = dict(
            court=data["court"], duration=data["duration"], customer_name=data["customer_name"],
            customer_phone=data["customer_phone"], customer_email=data["customer_email"],
            extras=form.selected_extras(), source=data["source"], players=data["players"], notes=data["notes"],
            created_by=request.user,
        )
        try:
            if data.get("repeat_weeks"):
                _, created, skipped = services.create_series(
                    first_date=data["business_date"], start_time=data["start_time"], weeks=data["repeat_weeks"],
                    **common,
                )
                if not created:
                    raise ValidationError(f"No weeks could be booked: {skipped[0][1]}")
                booking = created[0]
                msg = f"Weekly booking created: {len(created)} of {data['repeat_weeks']} weeks booked."
                if skipped:
                    msg += " Skipped (already taken): " + ", ".join(f"{d:%d %b}" for d, _ in skipped) + "."
            else:
                booking = services.create_booking(
                    business_date=data["business_date"],
                    start_at=services.combine(data["business_date"], data["start_time"], s),
                    promo_code=data["promo_code"], staff=True, **common,
                )
                skipped, msg = [], f"Booking {booking.reference} created."
        except ValidationError as exc:
            form.add_error(None, exc.messages[0])
        else:
            if data["collect_payment"] and booking.invoice.balance > 0:
                billing.record_payment(booking.invoice, amount=booking.invoice.balance,
                                       method=data["payment_method"] or "cash", received_by=request.user)
            (messages.warning if skipped else messages.success)(request, msg)
            return redirect("dashboard:booking_detail", pk=booking.pk)
    return render(request, "dashboard/booking_form.html", {"form": form, "nav": "bookings"})


@staff_required
def booking_detail(request, pk):
    booking = get_object_or_404(Booking.objects.select_related("court", "invoice", "promo_code", "created_by"), pk=pk)
    invoice = getattr(booking, "invoice", None)
    s = SiteSettings.load()
    return render(request, "dashboard/booking_detail.html", {
        "booking": booking,
        "invoice": invoice,
        "extras": booking.extras.select_related("extra"),
        "payments": invoice.payments.all() if invoice else [],
        "next_statuses": [(st, Booking.Status(st).label) for st in services.ALLOWED_TRANSITIONS[booking.status]],
        "payment_form": PaymentForm(initial={"amount": invoice.balance if invoice else 0}),
        "whatsapp_url": whatsapp_link(booking, s),
        "pay_link": pay_url(booking) if invoice and invoice.balance > 0 and s.gateway_ready else "",
        "notifications": booking.notifications.all(),
        "wallet": wallets.find_wallet(booking.customer_phone),
        "series_info": _series_info(booking),
        "online_payments": invoice.online_payments.all()[:10] if invoice else [],
        "nav": "bookings",
    })


@staff_required
@require_POST
def booking_status(request, pk):
    booking = get_object_or_404(Booking, pk=pk)
    try:
        booking = services.change_status(booking, request.POST.get("status", ""))
        messages.success(request, f"Booking marked as {booking.get_status_display().lower()}.")
    except (ValidationError, ValueError) as exc:
        messages.error(request, getattr(exc, "messages", [str(exc)])[0])
    return _back(request, "dashboard:booking_detail", pk=pk)


def _series_info(booking):
    if not booking.series_id:
        return None
    items = list(booking.series.bookings.order_by("start_at").values_list("pk", "status", "start_at"))
    now = timezone.now()
    return {
        "series": booking.series,
        "position": [pk for pk, _, _ in items].index(booking.pk) + 1,
        "total": len(items),
        "remaining": sum(1 for _, st, start in items if start > now and st not in Booking.INACTIVE_STATUSES),
    }


@staff_required
@require_POST
def booking_pay_credit(request, pk):
    booking = get_object_or_404(Booking.objects.select_related("invoice"), pk=pk)
    try:
        payment = wallets.pay_with_credit(booking.invoice, received_by=request.user)
        messages.success(request, f"Paid {payment.amount:,.0f} from club credit.")
    except ValidationError as exc:
        messages.error(request, exc.messages[0])
    return redirect("dashboard:booking_detail", pk=pk)


@owner_required
@require_POST
def booking_refund_credit(request, pk):
    booking = get_object_or_404(Booking, pk=pk)
    try:
        wallet = wallets.refund_booking_to_credit(booking, user=request.user)
        messages.success(request, f"Added to {wallet.name}'s club credit. New balance: {wallet.balance:,.0f}.")
    except ValidationError as exc:
        messages.error(request, exc.messages[0])
    return _back(request, "dashboard:booking_detail", pk=pk)


@staff_required
@require_POST
def series_cancel(request, pk):
    from bookings.models import BookingSeries

    series = get_object_or_404(BookingSeries, pk=pk)
    count = services.cancel_series_remaining(series)
    messages.success(request, f"Cancelled {count} upcoming week{'s' if count != 1 else ''} of this weekly booking.")
    return _back(request, "dashboard:booking_list")


@owner_required
@require_POST
def booking_refund_handled(request, pk):
    booking = get_object_or_404(Booking, pk=pk)
    booking.notes = (booking.notes + f"\n[refund handled] by {request.user.get_username()} "
                     f"on {timezone.localdate():%d %b %Y}").strip()
    booking.save(update_fields=["notes", "updated_at"])
    messages.success(request, "Marked as handled.")
    return _back(request, "dashboard:booking_detail", pk=pk)


# --------------------------------------------------------------------------- invoices
@staff_required
def invoice_list(request):
    qs = Invoice.objects.select_related("booking")
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(Q(number__icontains=q) | Q(bill_to_name__icontains=q) | Q(bill_to_phone__icontains=q)
                       | Q(booking__reference__icontains=q))
    status = request.GET.get("status")
    if status == "overdue":
        qs = qs.filter(status__in=["unpaid", "partial"], due_date__lt=timezone.localdate())
    elif status:
        qs = qs.filter(status=status)
    totals = qs.exclude(status="void").aggregate(total=Sum("total"), paid=Sum("amount_paid"))
    page = Paginator(qs, 25).get_page(request.GET.get("page"))
    return render(request, "dashboard/invoice_list.html", {
        "page": page,
        "statuses": Invoice.Status.choices,
        "totals": totals,
        "nav": "invoices",
    })


@staff_required
def invoice_create(request):
    form = InvoiceForm(request.POST or None)
    formset = InvoiceItemFormSet(request.POST or None, prefix="items")
    if request.method == "POST" and form.is_valid() and formset.is_valid():
        items = [(f.cleaned_data["description"], f.cleaned_data["quantity"], f.cleaned_data["unit_price"])
                 for f in formset if f.cleaned_data and not f.cleaned_data.get("DELETE")]
        if not items:
            form.add_error(None, "Add at least one line item.")
        else:
            data = form.cleaned_data
            invoice = billing.create_invoice(
                bill_to_name=data["bill_to_name"], bill_to_phone=data["bill_to_phone"],
                bill_to_email=data["bill_to_email"], discount=data["discount"] or Decimal("0"),
                due_date=data["due_date"], notes=data["notes"], items=items,
            )
            messages.success(request, f"Invoice {invoice.number} created.")
            return redirect("dashboard:invoice_detail", pk=invoice.pk)
    return render(request, "dashboard/invoice_form.html", {"form": form, "formset": formset, "nav": "invoices"})


@staff_required
def invoice_detail(request, pk):
    invoice = get_object_or_404(Invoice.objects.select_related("booking__court"), pk=pk)
    return render(request, "dashboard/invoice_detail.html", {
        "invoice": invoice,
        "items": invoice.items.all(),
        "payments": invoice.payments.select_related("received_by"),
        "payment_form": PaymentForm(initial={"amount": invoice.balance}),
        "nav": "invoices",
    })


@staff_required
@require_POST
def invoice_payment(request, pk):
    invoice = get_object_or_404(Invoice, pk=pk)
    form = PaymentForm(request.POST)
    if form.is_valid():
        try:
            billing.record_payment(invoice, received_by=request.user, **form.cleaned_data)
            messages.success(request, "Payment recorded.")
        except ValidationError as exc:
            messages.error(request, exc.messages[0])
    else:
        messages.error(request, "Please enter a valid payment amount.")
    return _back(request, "dashboard:invoice_detail", pk=pk)


@owner_required
@require_POST
def invoice_void(request, pk):
    invoice = get_object_or_404(Invoice, pk=pk)
    try:
        billing.void_invoice(invoice)
        messages.success(request, f"Invoice {invoice.number} voided.")
    except ValidationError as exc:
        messages.error(request, exc.messages[0])
    return redirect("dashboard:invoice_detail", pk=pk)


# --------------------------------------------------------------------------- customers
@staff_required
def customers(request):
    qs = Booking.objects.values("customer_phone").annotate(
        name=Max("customer_name"),
        email=Max("customer_email"),
        visits=Count("id", filter=~Q(status__in=Booking.INACTIVE_STATUSES)),
        no_shows=Count("id", filter=Q(status=Booking.Status.NO_SHOW)),
        last_visit=Max("start_at"),
        spent=Sum("invoice__amount_paid"),
    )
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(Q(customer_name__icontains=q) | Q(customer_phone__icontains=q))
    order = {"recent": "-last_visit", "spent": "-spent"}.get(request.GET.get("sort"), "-visits")
    page = Paginator(qs.order_by(order), 30).get_page(request.GET.get("page"))
    balances = {w.phone: w for w in Wallet.objects.filter(
        phone__in=[normalize_phone(row["customer_phone"]) for row in page])}
    for row in page:
        row["wallet"] = balances.get(normalize_phone(row["customer_phone"]))
    return render(request, "dashboard/customers.html", {"page": page, "nav": "customers"})


# --------------------------------------------------------------------------- catalog (courts / extras / promos)
CATALOG = {
    "courts": (Court, CourtForm, "Courts", ["name", "court_type", "price_off_peak", "price_peak", "is_active"]),
    "extras": (Extra, ExtraForm, "Extras & rentals", ["name", "price", "max_quantity", "is_active"]),
    "packages": (CreditPackage, CreditPackageForm, "Credit packages", ["name", "price", "credit", "is_active"]),
    "promos": (PromoCode, PromoForm, "Promo codes", ["code", "discount_type", "value", "times_used", "max_uses",
                                                     "valid_until", "is_active"]),
}


@owner_required
def catalog_list(request, kind):
    if kind not in CATALOG:
        raise Http404
    model, _, title, columns = CATALOG[kind]
    objects = model.objects.all()
    rows = [(obj, [getattr(obj, f"get_{c}_display", lambda c=c, o=obj: getattr(o, c))() for c in columns])
            for obj in objects]
    headers = [model._meta.get_field(c).verbose_name for c in columns]
    return render(request, "dashboard/catalog_list.html", {
        "kind": kind, "title": title, "rows": rows, "headers": headers, "nav": kind,
    })


@owner_required
def catalog_form(request, kind, pk=None):
    if kind not in CATALOG:
        raise Http404
    model, form_class, title, _ = CATALOG[kind]
    obj = get_object_or_404(model, pk=pk) if pk else None
    limit = request.club.max_courts
    if kind == "courts" and obj is None and limit is not None and Court.objects.count() >= limit:
        messages.error(request, f"Your {request.club.plan_info['label']} plan includes {limit} courts. "
                                "Upgrade to Pro for unlimited courts.")
        return redirect("dashboard:subscription")
    form = form_class(request.POST or None, request.FILES or None, instance=obj)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, f"{title[:-1] if title.endswith('s') else title} saved.")
        return redirect("dashboard:catalog_list", kind=kind)
    return render(request, "dashboard/catalog_form.html", {
        "form": form, "kind": kind, "title": title, "obj": obj, "nav": kind,
    })


@owner_required
def settings_view(request):
    form = SettingsForm(request.POST or None, instance=SiteSettings.load())
    if request.method == "POST" and "test_whatsapp" in request.POST:
        return _test_whatsapp(request)
    if request.method == "POST" and form.is_valid():
        saved = form.save()
        if saved.club_name != request.club.name:
            type(request.club).objects.filter(pk=request.club.pk).update(name=saved.club_name)
        messages.success(request, "Settings saved.")
        return redirect("dashboard:settings")
    return render(request, "dashboard/settings.html", {
        "form": form, "nav": "settings",
        "return_url": request.build_absolute_uri("/payments/return/jazzcash/"),
        "ipn_url": request.build_absolute_uri("/payments/jazzcash/ipn/"),
    })


def _test_whatsapp(request):
    """Send Meta's default `hello_world` template to the given number to check the connection."""
    club = SiteSettings.load()
    phone = request.POST.get("test_phone", "")
    if not (club.whatsapp_phone_number_id and club.whatsapp_access_token):
        messages.error(request, "Save your WhatsApp phone number ID and access token first.")
    elif not phone:
        messages.error(request, "Enter a phone number to send the test to.")
    else:
        try:
            send_template(club, normalize_phone(phone), "hello_world", [], language="en_US")
            messages.success(request, f"Test message sent to {phone}. Check WhatsApp.")
        except WhatsAppError as exc:
            messages.error(request, f"WhatsApp said: {exc}")
    return redirect("dashboard:settings")


# --------------------------------------------------------------------------- exports
def _csv_response(filename):
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


@owner_required
def export_bookings(request):
    response = _csv_response("bookings.csv")
    writer = csv.writer(response)
    writer.writerow(["Reference", "Date", "Start", "End", "Court", "Customer", "Phone", "Email", "Status",
                     "Source", "Court amount", "Extras", "Discount", "Invoice", "Invoice total", "Paid"])
    for b in _filtered_bookings(request).order_by("start_at"):
        inv = getattr(b, "invoice", None)
        writer.writerow([
            b.reference, b.business_date, timezone.localtime(b.start_at).strftime("%H:%M"),
            timezone.localtime(b.end_at).strftime("%H:%M"), b.court.name, b.customer_name, b.customer_phone,
            b.customer_email, b.get_status_display(), b.get_source_display(), b.court_amount, b.extras_amount,
            b.discount_amount, inv.number if inv else "", inv.total if inv else "", inv.amount_paid if inv else "",
        ])
    return response


@owner_required
def export_invoices(request):
    response = _csv_response("invoices.csv")
    writer = csv.writer(response)
    writer.writerow(["Number", "Issue date", "Due date", "Customer", "Phone", "Subtotal", "Discount", "Tax",
                     "Total", "Paid", "Balance", "Status", "Booking"])
    for inv in Invoice.objects.select_related("booking").order_by("issue_date", "number"):
        writer.writerow([
            inv.number, inv.issue_date, inv.due_date, inv.bill_to_name, inv.bill_to_phone, inv.subtotal,
            inv.discount, inv.tax_amount, inv.total, inv.amount_paid, inv.balance, inv.get_status_display(),
            inv.booking.reference if inv.booking else "",
        ])
    return response


# --------------------------------------------------------------------------- team & subscription
@owner_required
def team(request):
    from .forms import TeamMemberForm

    form = TeamMemberForm(request.POST or None, club=request.club)
    if request.method == "POST" and form.is_valid():
        member = form.save()
        messages.success(request, f"{member.user.get_full_name() or member.user.email} can now log in at "
                                  f"{request.club.host}.")
        return redirect("dashboard:team")
    members = Membership.objects.filter(club=request.club).select_related("user").order_by("role", "created_at")
    return render(request, "dashboard/team.html", {"form": form, "members": members, "nav": "team"})


@owner_required
@require_POST
def team_remove(request, pk):
    member = get_object_or_404(Membership, pk=pk, club=request.club)
    if member.user_id == request.user.id:
        messages.error(request, "You can't remove yourself.")
    elif member.role == Membership.Role.OWNER and Membership.objects.filter(
            club=request.club, role=Membership.Role.OWNER).count() == 1:
        messages.error(request, "A club needs at least one owner.")
    else:
        member.delete()
        messages.success(request, "Access removed.")
    return redirect("dashboard:team")


@club_member_required(allow_inactive=True)
def subscription(request):
    from django.conf import settings as dj_settings

    from tenants.plans import PLANS

    return render(request, "dashboard/subscription.html", {
        "plans": PLANS, "courts_used": Court.objects.count(), "nav": "subscription",
        "payment_instructions": dj_settings.PLATFORM_PAYMENT_INSTRUCTIONS,
        "support_whatsapp": dj_settings.PLATFORM_SUPPORT_WHATSAPP,
        "support_email": dj_settings.PLATFORM_SUPPORT_EMAIL,
    })


# --------------------------------------------------------------------------- club credit
@staff_required
def credit_list(request):
    qs = Wallet.objects.all()
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(phone__icontains=normalize_phone(q) or q))
    form = SellCreditForm(request.POST or None, initial={"name": request.GET.get("name", ""),
                                                         "phone": request.GET.get("phone", "")})
    if request.method == "POST" and form.is_valid():
        return _sell(request, form)
    totals = Wallet.objects.aggregate(balance=Sum("balance"), n=Count("id"))
    return render(request, "dashboard/credit_list.html", {
        "page": Paginator(qs.order_by("-balance", "name"), 30).get_page(request.GET.get("page")),
        "form": form, "totals": totals, "packages": CreditPackage.objects.filter(is_active=True), "nav": "credit",
    })


def _sell(request, form):
    data = form.cleaned_data
    try:
        wallet, invoice = wallets.sell_credit(
            phone=data["phone"], name=data["name"], method=data["method"], received_by=request.user,
            package=data["package"], price=data["price"], credit=data["credit"], reference=data["reference"],
        )
    except ValidationError as exc:
        messages.error(request, exc.messages[0])
        return redirect(request.path)
    messages.success(request, f"Credit added. {wallet.name}'s balance is now {wallet.balance:,.0f} "
                              f"(invoice {invoice.number}).")
    return redirect("dashboard:credit_detail", pk=wallet.pk)


@staff_required
def credit_detail(request, pk):
    wallet = get_object_or_404(Wallet, pk=pk)
    form = SellCreditForm(request.POST or None, initial={"name": wallet.name, "phone": wallet.phone})
    if request.method == "POST" and form.is_valid():
        return _sell(request, form)
    return render(request, "dashboard/credit_detail.html", {
        "wallet": wallet, "form": form, "adjust_form": AdjustCreditForm(),
        "transactions": wallet.transactions.select_related("invoice", "created_by")[:100],
        "bookings": Booking.objects.filter(customer_phone__endswith=wallet.phone[-10:])
        .select_related("court", "invoice").order_by("-start_at")[:10],
        "nav": "credit",
    })


@owner_required
@require_POST
def credit_adjust(request, pk):
    wallet = get_object_or_404(Wallet, pk=pk)
    form = AdjustCreditForm(request.POST)
    if form.is_valid():
        try:
            wallets.adjust_credit(wallet, form.cleaned_data["amount"], form.cleaned_data["note"], request.user)
            messages.success(request, "Balance adjusted.")
        except ValidationError as exc:
            messages.error(request, exc.messages[0])
    else:
        messages.error(request, "Enter an amount and a reason.")
    return redirect("dashboard:credit_detail", pk=pk)


# --------------------------------------------------------------------------- end-of-day cash report
@staff_required
def cash_report(request):
    s = SiteSettings.load()
    today = services.current_business_date(s)
    day = _parse_date(request.GET.get("date") or request.POST.get("date"), today)
    closed = DayClose.objects.filter(business_date=day).select_related("closed_by").first()
    form = DayCloseForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            close = reports.close_day(day, counted_cash=form.cleaned_data["counted_cash"],
                                      note=form.cleaned_data["note"], user=request.user)
        except ValidationError as exc:
            messages.error(request, exc.messages[0])
        else:
            diff = close.difference
            if diff == 0:
                messages.success(request, "Day closed — cash matches exactly.")
            else:
                messages.warning(request, f"Day closed with a {'surplus' if diff > 0 else 'shortage'} of "
                                          f"{abs(diff):,.0f}. The owner will see this.")
        return redirect(f"{request.path}?date={day.isoformat()}")
    history = []
    if request.club_role == "owner":
        history = DayClose.objects.select_related("closed_by")[:30]
    return render(request, "dashboard/cash_report.html", {
        "day": day, "today": today, "prev_day": day - timedelta(days=1), "next_day": day + timedelta(days=1),
        "summary": reports.day_summary(day), "closed": closed, "form": form, "history": history,
        "nav": "cash",
    })


@owner_required
@require_POST
def cash_reopen(request, pk):
    close = get_object_or_404(DayClose, pk=pk)
    day = close.business_date
    close.delete()
    messages.success(request, f"{day:%d %b} reopened — it can be counted again.")
    return redirect(f"{reverse('dashboard:cash_report')}?date={day.isoformat()}")
