from datetime import time, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from billing.models import Invoice
from billing.services import record_payment, void_invoice
from core.models import SiteSettings
from courts.models import Court
from tenants.context import reset_current_club, set_current_club
from tenants.models import Club, Membership

from . import services
from .models import Booking, Extra, PromoCode


def next_weekday(weekday, min_days=2):
    d = timezone.localdate() + timedelta(days=min_days)
    while d.weekday() != weekday:
        d += timedelta(days=1)
    return d


class BaseCase(TestCase):
    """Every test runs inside one club, reachable at the test client's default host."""

    def setUp(self):
        self.club = Club.objects.create(name="Test Club", slug="testclub", custom_domain="testserver",
                                        paid_until=timezone.localdate() + timedelta(days=30))
        token = set_current_club(self.club)
        self.addCleanup(reset_current_club, token)
        self.s = SiteSettings.load()
        self.s.open_time, self.s.close_time = time(8), time(2)
        self.s.slot_minutes = 30
        self.s.allowed_durations = "60,90,120"
        self.s.peak_start, self.s.peak_end = time(17), time(1)
        self.s.weekend_all_day_peak = True
        self.s.tax_percent = Decimal("0")
        self.s.save()
        self.court = Court.objects.create(name="Court A", price_off_peak=4000, price_peak=6000)
        self.monday = next_weekday(0)
        self.saturday = next_weekday(5)

    def member(self, email, role="owner", **extra):
        user = get_user_model().objects.create_user(email, email, "pass12345", **extra)
        Membership.objects.create(user=user, club=self.club, role=role)
        return user

    def book(self, day=None, at=time(10), duration=60, **kw):
        day = day or self.monday
        defaults = dict(court=self.court, business_date=day, start_at=services.combine(day, at, self.s),
                        duration=duration, customer_name="Ali Raza", customer_phone="0300 1234567")
        defaults.update(kw)
        return services.create_booking(**defaults)


class PricingTests(BaseCase):
    def test_off_peak_price(self):
        b = self.book(at=time(10), duration=90)
        self.assertEqual(b.court_amount, Decimal("6000.00"))

    def test_slot_crossing_into_peak_is_split(self):
        b = self.book(at=time(16, 30), duration=60)  # 30 min off-peak + 30 min peak
        self.assertEqual(b.court_amount, Decimal("5000.00"))

    def test_weekend_is_all_day_peak(self):
        b = self.book(day=self.saturday, at=time(9), duration=60)
        self.assertEqual(b.court_amount, Decimal("6000.00"))

    def test_peak_window_crossing_midnight(self):
        b = self.book(at=time(0, 30), duration=60)  # 00:30-01:00 peak, 01:00-01:30 off-peak
        self.assertEqual(b.court_amount, Decimal("5000.00"))


class OpeningHoursTests(BaseCase):
    def test_after_midnight_slot_belongs_to_business_day(self):
        b = self.book(at=time(0, 30), duration=60)
        self.assertEqual(b.business_date, self.monday)
        self.assertEqual(timezone.localtime(b.start_at).date(), self.monday + timedelta(days=1))

    def test_slot_grid_runs_until_close(self):
        slots = services.day_slots(self.court, self.monday, self.s)
        self.assertEqual(len(slots), 36)  # 08:00 -> 02:00 in 30 min slots
        self.assertEqual(slots[-1].value, "01:30")

    def test_cannot_run_past_closing(self):
        with self.assertRaises(ValidationError):
            self.book(at=time(1, 30), duration=60)

    def test_outside_hours_rejected(self):
        with self.assertRaises(ValidationError):
            self.book(at=time(6), duration=60)

    def test_misaligned_start_rejected(self):
        with self.assertRaises(ValidationError):
            self.book(at=time(10, 15), duration=60)

    def test_duration_must_be_allowed_online(self):
        with self.assertRaises(ValidationError):
            self.book(duration=30)

    def test_past_slot_rejected_online(self):
        today = services.current_business_date(self.s)
        with self.assertRaises(ValidationError):
            services.create_booking(court=self.court, business_date=today - timedelta(days=1),
                                    start_at=services.combine(today - timedelta(days=1), time(10), self.s),
                                    duration=60, customer_name="X", customer_phone="03001234567")

    def test_booking_window(self):
        far = services.current_business_date(self.s) + timedelta(days=self.s.booking_window_days + 1)
        with self.assertRaises(ValidationError):
            self.book(day=far)

    def test_inactive_court(self):
        self.court.is_active = False
        self.court.save()
        with self.assertRaises(ValidationError):
            self.book()


class ConflictTests(BaseCase):
    def test_overlap_rejected(self):
        self.book(at=time(10), duration=90)
        with self.assertRaises(ValidationError):
            self.book(at=time(11), duration=60)

    def test_back_to_back_allowed(self):
        self.book(at=time(10), duration=60)
        self.book(at=time(11), duration=60)
        self.assertEqual(Booking.objects.count(), 2)

    def test_other_court_same_time_allowed(self):
        other = Court.objects.create(name="Court B", price_off_peak=1, price_peak=1)
        self.book()
        self.book(court=other)
        self.assertEqual(Booking.objects.count(), 2)

    def test_cancelled_booking_frees_slot(self):
        b = self.book()
        services.cancel_booking(b, staff=True)
        self.book()
        self.assertEqual(Booking.objects.active().count(), 1)

    def test_grid_marks_booked(self):
        self.book(at=time(10), duration=60)
        slots = {s.value: s.status for s in services.day_slots(self.court, self.monday, self.s)}
        self.assertEqual(slots["10:00"], "booked")
        self.assertEqual(slots["10:30"], "booked")
        self.assertEqual(slots["11:00"], "available")


class PromoAndExtrasTests(BaseCase):
    def test_percent_promo_and_usage_count(self):
        PromoCode.objects.create(code="welcome10", discount_type="percent", value=10)
        b = self.book(promo_code="WELCOME10")
        self.assertEqual(b.discount_amount, Decimal("400.00"))
        self.assertEqual(b.invoice.total, Decimal("3600.00"))
        self.assertEqual(PromoCode.objects.get().times_used, 1)
        services.cancel_booking(b, staff=True)
        self.assertEqual(PromoCode.objects.get().times_used, 0)

    def test_promo_limits(self):
        PromoCode.objects.create(code="ONCE", discount_type="fixed", value=500, max_uses=1)
        self.book(promo_code="ONCE")
        with self.assertRaises(ValidationError):
            self.book(at=time(12), promo_code="ONCE")

    def test_unknown_promo(self):
        with self.assertRaises(ValidationError):
            self.book(promo_code="NOPE")
        self.assertEqual(Booking.objects.count(), 0)  # whole booking rolled back

    def test_extras_on_invoice(self):
        racket = Extra.objects.create(name="Racket", price=500)
        b = self.book(extras=[(racket, 2)])
        self.assertEqual(b.extras_amount, Decimal("1000"))
        self.assertEqual(b.invoice.total, Decimal("5000.00"))
        self.assertEqual(b.invoice.items.count(), 2)


class CancellationTests(BaseCase):
    def test_customer_can_cancel_early(self):
        b = self.book()
        services.cancel_booking(b)
        b.refresh_from_db()
        self.assertEqual(b.status, Booking.Status.CANCELLED)
        self.assertEqual(b.invoice.status, Invoice.Status.VOID)

    def test_customer_cannot_cancel_late(self):
        b = self.book()
        with self.assertRaises(ValidationError):
            services.cancel_booking(b, now=b.start_at - timedelta(hours=1))

    def test_paid_invoice_not_voided_on_cancel(self):
        b = self.book()
        record_payment(b.invoice, amount=1000)
        services.cancel_booking(b, staff=True)
        b.invoice.refresh_from_db()
        self.assertEqual(b.invoice.status, Invoice.Status.PARTIAL)

    def test_status_transitions(self):
        b = self.book()
        services.change_status(b, Booking.Status.CHECKED_IN)
        services.change_status(b, Booking.Status.COMPLETED)
        with self.assertRaises(ValidationError):
            services.change_status(b, Booking.Status.CONFIRMED)


class InvoiceTests(BaseCase):
    def test_sequential_numbers(self):
        b1, b2 = self.book(), self.book(at=time(12))
        year = timezone.localdate().year
        self.assertEqual(b1.invoice.number, f"INV-{year}-00001")
        self.assertEqual(b2.invoice.number, f"INV-{year}-00002")

    def test_tax_applied_after_discount(self):
        self.s.tax_percent = Decimal("16")
        self.s.save()
        PromoCode.objects.create(code="OFF", discount_type="fixed", value=1000)
        b = self.book(promo_code="OFF")
        self.assertEqual(b.invoice.tax_amount, Decimal("480.00"))  # 16% of (4000-1000)
        self.assertEqual(b.invoice.total, Decimal("3480.00"))

    def test_payments_drive_status(self):
        inv = self.book().invoice
        record_payment(inv, amount=1500, method="cash")
        inv.refresh_from_db()
        self.assertEqual(inv.status, Invoice.Status.PARTIAL)
        self.assertEqual(inv.balance, Decimal("2500.00"))
        with self.assertRaises(ValidationError):
            record_payment(inv, amount=5000)
        record_payment(inv, amount=2500, method="jazzcash")
        inv.refresh_from_db()
        self.assertEqual(inv.status, Invoice.Status.PAID)

    def test_cannot_void_paid_invoice(self):
        inv = self.book().invoice
        record_payment(inv, amount=100)
        with self.assertRaises(ValidationError):
            void_invoice(inv)

    def test_pending_booking_confirmed_when_paid(self):
        self.s.auto_confirm = False
        self.s.save()
        b = self.book()
        self.assertEqual(b.status, Booking.Status.PENDING)
        record_payment(b.invoice, amount=b.invoice.total)
        b.refresh_from_db()
        self.assertEqual(b.status, Booking.Status.CONFIRMED)


class PublicFlowTests(BaseCase):
    def test_full_online_booking_flow(self):
        r = self.client.get(reverse("bookings:book") + f"?date={self.monday.isoformat()}")
        self.assertContains(r, "Court A")
        params = {"court": self.court.slug, "date": self.monday.isoformat(), "start": "18:00", "duration": "90"}
        r = self.client.get(reverse("bookings:details"), params)
        self.assertContains(r, "Booking summary")
        r = self.client.post(reverse("bookings:details"), {
            **params, "customer_name": "Sara Ahmed", "customer_phone": "0321 7654321", "customer_email": "sara@x.pk",
            "players": 4, "promo_code": "",
        })
        booking = Booking.objects.get()
        self.assertRedirects(r, reverse("bookings:detail", args=[booking.reference]))
        self.assertEqual(booking.court_amount, Decimal("9000.00"))
        r = self.client.get(reverse("bookings:detail", args=[booking.reference]))
        self.assertContains(r, booking.reference)
        self.assertContains(r, "View invoice")

    def test_invalid_promo_shows_error(self):
        params = {"court": self.court.slug, "date": self.monday.isoformat(), "start": "10:00", "duration": "60"}
        r = self.client.post(reverse("bookings:details"), {**params, "customer_name": "A", "customer_phone":
                                                           "03001234567", "players": 4, "promo_code": "BAD"})
        self.assertContains(r, "doesn&#x27;t exist")
        self.assertEqual(Booking.objects.count(), 0)

    def test_strangers_cannot_view_booking_but_lookup_works(self):
        b = self.book()
        url = reverse("bookings:detail", args=[b.reference])
        self.assertEqual(self.client.get(url).status_code, 302)
        r = self.client.post(reverse("bookings:manage"), {"reference": b.reference.lower(), "phone": "+92 300 1234567"})
        self.assertRedirects(r, url)
        self.assertEqual(self.client.get(url).status_code, 200)

    def test_public_invoice(self):
        inv = self.book().invoice
        r = self.client.get(reverse("billing:public_invoice", args=[inv.token]))
        self.assertContains(r, inv.number)


class DashboardTests(BaseCase):
    def setUp(self):
        super().setUp()
        User = get_user_model()
        self.staff = self.member("staff@club.pk")
        self.player = User.objects.create_user("p@club.pk", "p@club.pk", "pass12345")

    def test_dashboard_requires_staff(self):
        url = reverse("dashboard:overview")
        self.assertEqual(self.client.get(url).status_code, 302)
        self.client.force_login(self.player)
        self.assertEqual(self.client.get(url).status_code, 302)
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get(url).status_code, 200)

    def test_walk_in_with_payment(self):
        self.client.force_login(self.staff)
        r = self.client.post(reverse("dashboard:booking_create"), {
            "court": self.court.pk, "business_date": self.monday.isoformat(), "start_time": "20:00",
            "duration": 60, "customer_name": "Walk In", "customer_phone": "03111111111", "source": "walk_in",
            "players": 4, "collect_payment": "on", "payment_method": "cash",
        })
        b = Booking.objects.get()
        self.assertRedirects(r, reverse("dashboard:booking_detail", args=[b.pk]))
        self.assertEqual(b.invoice.status, Invoice.Status.PAID)
        self.assertEqual(b.created_by, self.staff)

    def test_pages_render(self):
        b = self.book()
        self.client.force_login(self.staff)
        for name, args in [("overview", []), ("schedule", []), ("booking_list", []), ("booking_create", []),
                           ("booking_detail", [b.pk]), ("invoice_list", []), ("invoice_create", []),
                           ("invoice_detail", [b.invoice.pk]), ("customers", []), ("settings", []),
                           ("catalog_list", ["courts"]), ("catalog_create", ["promos"])]:
            self.assertEqual(self.client.get(reverse(f"dashboard:{name}", args=args)).status_code, 200, name)

    def test_manual_invoice(self):
        self.client.force_login(self.staff)
        r = self.client.post(reverse("dashboard:invoice_create"), {
            "bill_to_name": "Corporate Event", "discount": "0",
            "items-TOTAL_FORMS": "2", "items-INITIAL_FORMS": "0", "items-MIN_NUM_FORMS": "1",
            "items-MAX_NUM_FORMS": "1000",
            "items-0-description": "Tournament entry", "items-0-quantity": "8", "items-0-unit_price": "2000",
            "items-1-description": "", "items-1-quantity": "1", "items-1-unit_price": "",
        })
        inv = Invoice.objects.get()
        self.assertRedirects(r, reverse("dashboard:invoice_detail", args=[inv.pk]))
        self.assertEqual(inv.total, Decimal("16000.00"))
