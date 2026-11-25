"""Club credit, weekly recurring bookings and the end-of-day cash report."""
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from billing import reports, wallets
from billing.models import CreditPackage, DayClose, Invoice, Payment, Wallet, WalletTransaction
from billing.services import record_payment
from tenants.context import use_club
from tenants.models import Club

from . import services
from .models import Booking, BookingSeries
from .tests import BaseCase

PHONE = "0300 1234567"  # BaseCase.book() default customer


class CreditTests(BaseCase):
    def setUp(self):
        super().setUp()
        self.gold = CreditPackage.objects.create(name="Gold", price=Decimal("45000"), credit=Decimal("50000"))
        self.s.tax_percent = Decimal("16")
        self.s.save()

    def test_selling_a_package(self):
        wallet, invoice = wallets.sell_credit(phone=PHONE, name="Ali Raza", method="cash", package=self.gold)
        self.assertEqual(wallet.phone, "923001234567")
        self.assertEqual(wallet.balance, Decimal("50000"))
        self.assertEqual((invoice.total, invoice.tax_amount, invoice.status), (Decimal("45000"), 0, "paid"))
        self.assertEqual(wallet.transactions.get().kind, WalletTransaction.Kind.PURCHASE)

    def test_custom_top_up_defaults_credit_to_amount_paid(self):
        wallet, _ = wallets.sell_credit(phone=PHONE, name="Ali", method="jazzcash", price=Decimal("10000"))
        wallets.sell_credit(phone="+92 300 1234567", name="Ali", method="cash", price=5000, credit=5500)
        wallet.refresh_from_db()
        self.assertEqual(wallet.balance, Decimal("15500"))  # same customer, number written differently

    def test_cannot_buy_credit_with_credit(self):
        with self.assertRaises(ValidationError):
            wallets.sell_credit(phone=PHONE, name="Ali", method="wallet", price=100)

    def test_pay_booking_from_credit(self):
        wallets.sell_credit(phone=PHONE, name="Ali", method="cash", package=self.gold)
        b = self.book()  # 4000 + 16% = 4640
        payment = wallets.pay_with_credit(b.invoice)
        b.invoice.refresh_from_db()
        self.assertEqual((payment.method, payment.amount, b.invoice.status), ("wallet", Decimal("4640"), "paid"))
        self.assertEqual(Wallet.objects.get().balance, Decimal("45360"))

    def test_partial_when_credit_runs_low(self):
        wallets.sell_credit(phone=PHONE, name="Ali", method="cash", price=1000)
        b = self.book()
        wallets.pay_with_credit(b.invoice)
        b.invoice.refresh_from_db()
        self.assertEqual((b.invoice.status, b.invoice.balance), ("partial", Decimal("3640")))
        self.assertEqual(Wallet.objects.get().balance, 0)
        with self.assertRaises(ValidationError):
            wallets.pay_with_credit(b.invoice)

    def test_no_wallet(self):
        with self.assertRaises(ValidationError):
            wallets.pay_with_credit(self.book().invoice)

    def test_balance_can_never_go_negative(self):
        wallet, _ = wallets.sell_credit(phone=PHONE, name="Ali", method="cash", price=100)
        with self.assertRaises(ValidationError):
            wallets.adjust_credit(wallet, Decimal("-500"), "oops")
        with self.assertRaises(IntegrityError), transaction.atomic():
            Wallet.objects.filter(pk=wallet.pk).update(balance=-1)

    def test_refund_cancelled_booking_to_credit(self):
        b = self.book()
        record_payment(b.invoice, amount=b.invoice.total, method="cash")
        with self.assertRaises(ValidationError):
            wallets.refund_booking_to_credit(b)  # still active
        services.cancel_booking(b, staff=True)
        wallet = wallets.refund_booking_to_credit(b)
        self.assertEqual(wallet.balance, Decimal("4640"))
        with self.assertRaises(ValidationError):
            wallets.refund_booking_to_credit(b)  # only once

    def test_credit_spend_is_not_new_money(self):
        wallets.sell_credit(phone=PHONE, name="Ali", method="cash", price=Decimal("10000"))
        wallets.pay_with_credit(self.book().invoice)
        summary = reports.day_summary(services.current_business_date(self.s))
        self.assertEqual(summary["collected"], Decimal("10000"))  # the top-up, not the spend
        self.assertEqual(summary["credit_used"], Decimal("4640"))

    def test_wallets_are_per_club(self):
        wallets.sell_credit(phone=PHONE, name="Ali", method="cash", price=1000)
        other = Club.objects.create(name="Other", slug="other")
        with use_club(other):
            self.assertIsNone(wallets.find_wallet(PHONE))
            wallets.sell_credit(phone=PHONE, name="Ali", method="cash", price=50)
        self.assertEqual(Wallet.all_clubs.count(), 2)

    def test_dashboard_permissions(self):
        staff = self.member("desk@club.pk", role="staff")
        owner = self.member("boss@club.pk", role="owner")
        self.client.force_login(staff)
        r = self.client.post(reverse("dashboard:credit_list"), {
            "name": "Ali", "phone": PHONE, "package": self.gold.pk, "method": "cash"})
        wallet = Wallet.objects.get()
        self.assertRedirects(r, reverse("dashboard:credit_detail", args=[wallet.pk]))
        b = self.book()
        self.assertContains(self.client.get(reverse("dashboard:booking_detail", args=[b.pk])), "Pay from credit")
        self.client.post(reverse("dashboard:booking_pay_credit", args=[b.pk]))
        b.invoice.refresh_from_db()
        self.assertEqual(b.invoice.status, Invoice.Status.PAID)
        # Staff can't change balances or turn payments into credit
        self.assertEqual(self.client.post(reverse("dashboard:credit_adjust", args=[wallet.pk]),
                                          {"amount": "100", "note": "x"}).status_code, 403)
        self.assertEqual(self.client.post(reverse("dashboard:booking_refund_credit", args=[b.pk])).status_code, 403)
        self.client.force_login(owner)
        self.client.post(reverse("dashboard:credit_adjust", args=[wallet.pk]), {"amount": "100", "note": "goodwill"})
        wallet.refresh_from_db()
        self.assertEqual(wallet.balance, Decimal("45460"))
        self.assertEqual(wallet.transactions.first().created_by, owner)

    @override_settings(PAYMENTS_DEMO_ENABLED=True)
    def test_credit_confirms_a_deposit_hold(self):
        self.s.prepayment_mode, self.s.payment_gateway = "deposit", "demo"
        self.s.save()
        wallets.sell_credit(phone=PHONE, name="Ali", method="cash", price=10000)
        b = self.book()
        self.assertEqual(b.status, Booking.Status.AWAITING_PAYMENT)
        wallets.pay_with_credit(b.invoice)
        b.refresh_from_db()
        self.assertEqual(b.status, Booking.Status.CONFIRMED)


class WeeklySeriesTests(BaseCase):
    def series(self, weeks=4, **kw):
        defaults = dict(court=self.court, first_date=self.monday, start_time=time(20), duration=90, weeks=weeks,
                        customer_name="Tuesday Group", customer_phone="03004445566")
        defaults.update(kw)
        return services.create_series(**defaults)

    def test_books_every_week(self):
        series, created, skipped = self.series()
        self.assertEqual((len(created), skipped), (4, []))
        self.assertEqual([b.business_date for b in created], [self.monday + timedelta(weeks=i) for i in range(4)])
        self.assertTrue(all(b.series_id == series.pk and b.invoice for b in created))
        self.assertEqual(series.get_weekday_display(), "Monday")

    def test_taken_weeks_are_skipped(self):
        self.book(day=self.monday + timedelta(weeks=2), at=time(20, 30), staff=True)
        _, created, skipped = self.series()
        self.assertEqual(len(created), 3)
        self.assertEqual(skipped[0][0], self.monday + timedelta(weeks=2))

    def test_cancel_remaining(self):
        series, created, _ = self.series()
        Booking.objects.filter(pk=created[0].pk).update(start_at=timezone.now() - timedelta(days=1))
        self.assertEqual(services.cancel_series_remaining(series), 3)
        self.assertEqual(series.bookings.active().count(), 1)

    def test_week_limits(self):
        with self.assertRaises(ValidationError):
            self.series(weeks=1)
        with self.assertRaises(ValidationError):
            self.series(weeks=60)

    def test_nothing_bookable_leaves_no_series(self):
        for i in range(4):
            self.book(day=self.monday + timedelta(weeks=i), at=time(20, 30), staff=True)
        series, created, skipped = self.series()
        self.assertIsNone(series)
        self.assertEqual((len(created), len(skipped)), (0, 4))
        self.assertFalse(BookingSeries.objects.exists())

    def test_dashboard_weekly_booking(self):
        self.client.force_login(self.member("desk@club.pk", role="staff"))
        self.book(day=self.monday + timedelta(weeks=1), at=time(20), staff=True)
        r = self.client.post(reverse("dashboard:booking_create"), {
            "court": self.court.pk, "business_date": self.monday.isoformat(), "start_time": "20:00",
            "duration": 60, "customer_name": "League", "customer_phone": "03111111111", "source": "phone",
            "players": 4, "repeat_weeks": 4,
        }, follow=True)
        self.assertContains(r, "3 of 4 weeks booked")
        self.assertContains(r, "Skipped")
        self.assertContains(r, "week 1 of 3")


class CashReportTests(BaseCase):
    def pay(self, amount, method="cash", when=None, user=None):
        inv = self.book(at=time(10 + Payment.objects.count() % 12)).invoice
        p = record_payment(inv, amount=amount, method=method, received_by=user)
        if when:
            Payment.objects.filter(pk=p.pk).update(paid_at=when)
        return p

    def local(self, day, hour, minute=0):
        return timezone.make_aware(datetime.combine(day, time(hour, minute)))

    def test_late_night_payments_count_for_the_evening(self):
        day = self.monday  # club open 08:00 - 02:00
        self.pay(1000, when=self.local(day, 9))
        self.pay(2000, when=self.local(day + timedelta(days=1), 1, 30))  # 1:30 AM after the evening session
        self.pay(4000, when=self.local(day + timedelta(days=1), 9))      # next business day
        summary = reports.day_summary(day)
        self.assertEqual(summary["expected_cash"], Decimal("3000"))

    def test_summary_and_close(self):
        staff = self.member("desk@club.pk", role="staff", first_name="Hira")
        day = services.current_business_date(self.s)
        self.pay(3000, user=staff)
        self.pay(2000, method="jazzcash", user=staff)
        summary = reports.day_summary(day)
        self.assertEqual((summary["expected_cash"], summary["collected"]), (Decimal("3000"), Decimal("5000")))
        self.assertEqual(summary["staff"][0][1]["cash"], Decimal("3000"))
        close = reports.close_day(day, counted_cash=Decimal("2500"), note="short", user=staff)
        self.assertEqual(close.difference, Decimal("-500"))
        with self.assertRaises(ValidationError):
            reports.close_day(day, counted_cash=1)

    def test_dashboard_close_and_reopen(self):
        staff = self.member("desk@club.pk", role="staff")
        owner = self.member("boss@club.pk", role="owner")
        self.pay(3000, user=staff)
        self.client.force_login(staff)
        url = reverse("dashboard:cash_report")
        r = self.client.post(url, {"counted_cash": "3000"}, follow=True)
        self.assertContains(r, "cash matches exactly")
        self.assertNotContains(r, "Recent closes")  # history is owner-only
        close = DayClose.objects.get()
        self.assertEqual(self.client.post(reverse("dashboard:cash_reopen", args=[close.pk])).status_code, 403)
        self.client.force_login(owner)
        self.assertContains(self.client.get(url), "Recent closes")
        self.client.post(reverse("dashboard:cash_reopen", args=[close.pk]))
        self.assertFalse(DayClose.objects.exists())

    def test_overview_nags_about_uncounted_cash(self):
        owner = self.member("boss@club.pk")
        yesterday = services.current_business_date(self.s) - timedelta(days=1)
        self.pay(1500, when=self.local(yesterday, 18))
        self.client.force_login(owner)
        self.assertContains(self.client.get(reverse("dashboard:overview")), "been counted yet")
        reports.close_day(yesterday, counted_cash=1500)
        self.assertNotContains(self.client.get(reverse("dashboard:overview")), "been counted yet")

    def test_overview_revenue_ignores_credit_spend(self):
        owner = self.member("boss@club.pk")
        wallets.sell_credit(phone=PHONE, name="Ali", method="cash", price=Decimal("10000"))
        wallets.pay_with_credit(self.book().invoice)
        self.client.force_login(owner)
        r = self.client.get(reverse("dashboard:overview"))
        self.assertEqual(r.context["revenue_today"], Decimal("10000"))
