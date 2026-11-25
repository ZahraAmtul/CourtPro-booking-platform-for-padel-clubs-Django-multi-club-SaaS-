"""End-of-day cash report. A cash day runs from the previous closing time to this day's closing
time, so payments taken at 1 AM after a late session count towards the evening they belong to, and
nothing taken while the club is shut falls between two days."""
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from bookings.services import opening_window
from core.models import SiteSettings

from .models import DayClose, Payment


def business_day_bounds(day, s=None):
    s = s or SiteSettings.load()
    return opening_window(day - timedelta(days=1), s)[1], opening_window(day, s)[1]


def day_summary(day):
    start, end = business_day_bounds(day)
    payments = list(
        Payment.objects.filter(paid_at__gte=start, paid_at__lt=end)
        .select_related("invoice", "invoice__booking", "received_by").order_by("paid_at")
    )
    by_method = defaultdict(lambda: {"count": 0, "total": Decimal("0")})
    by_staff = defaultdict(lambda: {"count": 0, "total": Decimal("0"), "cash": Decimal("0")})
    for p in payments:
        by_method[p.method]["count"] += 1
        by_method[p.method]["total"] += p.amount
        who = (p.received_by.get_full_name() or p.received_by.email) if p.received_by else "Online / system"
        by_staff[who]["count"] += 1
        by_staff[who]["total"] += p.amount
        if p.method == Payment.Method.CASH:
            by_staff[who]["cash"] += p.amount
    methods = [
        {"method": m, "label": Payment.Method(m).label, **v, "is_cash_in": m in Payment.CASH_IN_METHODS}
        for m, v in sorted(by_method.items(), key=lambda kv: -kv[1]["total"])
    ]
    return {
        "start": start, "end": end, "payments": payments, "methods": methods,
        "staff": sorted(by_staff.items(), key=lambda kv: -kv[1]["total"]),
        "expected_cash": by_method[Payment.Method.CASH]["total"] if Payment.Method.CASH in by_method else Decimal("0"),
        "collected": sum((m["total"] for m in methods if m["is_cash_in"]), Decimal("0")),
        "credit_used": by_method[Payment.Method.WALLET]["total"] if Payment.Method.WALLET in by_method else Decimal("0"),
    }


def close_day(day, *, counted_cash, note="", user=None):
    summary = day_summary(day)
    try:
        with transaction.atomic():
            return DayClose.objects.create(
                business_date=day, expected_cash=summary["expected_cash"], counted_cash=Decimal(counted_cash),
                totals={m["method"]: str(m["total"]) for m in summary["methods"]}, note=note[:300], closed_by=user,
            )
    except IntegrityError:
        raise ValidationError("This day is already closed.")
