from datetime import time, timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from billing.gateways import DemoGateway, JazzCashGateway, jazzcash_hash
from billing.models import Invoice, OnlinePayment
from billing.services import record_payment
from bookings.notifications import due_reminders, send_due_reminders, send_notification
from core.models import SiteSettings

from . import services
from .models import Booking, Notification, PromoCode
from .tests import BaseCase


def demo_token(client_response, which="success_token"):
    return client_response.context[which]


@override_settings(PAYMENTS_DEMO_ENABLED=True)
class PrepaymentTests(BaseCase):
    def setUp(self):
        super().setUp()
        self.s.prepayment_mode, self.s.deposit_percent, self.s.payment_gateway = "deposit", 50, "demo"
        self.s.payment_hold_minutes = 15
        self.s.save()

    def test_online_booking_waits_for_deposit(self):
        b = self.book()
        self.assertEqual(b.status, Booking.Status.AWAITING_PAYMENT)
        self.assertEqual(b.prepayment_amount, Decimal("2000"))
        self.assertAlmostEqual((b.payment_due_at - timezone.now()).total_seconds(), 900, delta=5)

    def test_deposit_rounds_up_and_includes_tax(self):
        self.s.tax_percent, self.s.deposit_percent = Decimal("16"), 33
        self.s.save()
        b = self.book()  # total 4640 -> 33% = 1531.2 -> 1532
        self.assertEqual(b.prepayment_amount, Decimal("1532"))

    def test_full_mode(self):
        self.s.prepayment_mode = "full"
        self.s.save()
        self.assertEqual(self.book().prepayment_amount, Decimal("4000.00"))

    def test_no_gateway_means_pay_at_club(self):
        self.s.payment_gateway = ""
        self.s.save()
        self.assertEqual(self.book().status, Booking.Status.CONFIRMED)

    @override_settings(PAYMENTS_DEMO_ENABLED=False)
    def test_demo_gateway_disabled_in_production(self):
        self.assertEqual(self.book().status, Booking.Status.CONFIRMED)

    def test_staff_bookings_never_need_prepayment(self):
        self.assertEqual(self.book(staff=True).status, Booking.Status.CONFIRMED)

    def test_free_booking_skips_payment(self):
        PromoCode.objects.create(code="FREE", discount_type="percent", value=100)
        self.assertEqual(self.book(promo_code="FREE").status, Booking.Status.CONFIRMED)

    def test_hold_blocks_slot_until_it_lapses(self):
        b = self.book()
        with self.assertRaises(ValidationError):
            self.book(customer_name="Other")
        Booking.objects.filter(pk=b.pk).update(payment_due_at=timezone.now() - timedelta(seconds=1))
        # Released immediately, even before the expiry job runs
        other = self.book(customer_name="Other")
        self.assertEqual(other.status, Booking.Status.AWAITING_PAYMENT)

    def test_expiry_job(self):
        PromoCode.objects.create(code="TEN", discount_type="percent", value=10)
        b = self.book(promo_code="TEN")
        self.assertEqual(services.expire_unpaid_bookings(now=b.payment_due_at + timedelta(seconds=1)), 1)
        b.refresh_from_db()
        self.assertEqual(b.status, Booking.Status.EXPIRED)
        self.assertEqual(b.invoice.status, Invoice.Status.VOID)
        self.assertEqual(PromoCode.objects.get().times_used, 0)

    def test_cash_at_desk_confirms_hold(self):
        b = self.book()
        record_payment(b.invoice, amount=2000, method="cash")
        b.refresh_from_db()
        self.assertEqual(b.status, Booking.Status.CONFIRMED)
        self.assertIsNone(b.payment_due_at)

    def test_partial_cash_below_deposit_keeps_hold(self):
        b = self.book()
        record_payment(b.invoice, amount=500, method="cash")
        b.refresh_from_db()
        self.assertEqual(b.status, Booking.Status.AWAITING_PAYMENT)

    def test_customer_can_release_hold(self):
        b = self.book()
        services.cancel_booking(b)
        b.refresh_from_db()
        self.assertEqual(b.status, Booking.Status.CANCELLED)


@override_settings(PAYMENTS_DEMO_ENABLED=True)
class OnlinePaymentFlowTests(BaseCase):
    def setUp(self):
        super().setUp()
        self.s.prepayment_mode, self.s.deposit_percent, self.s.payment_gateway = "deposit", 50, "demo"
        self.s.save()

    def start_booking(self):
        params = {"court": self.court.slug, "date": self.monday.isoformat(), "start": "10:00", "duration": "60"}
        r = self.client.post(reverse("bookings:details"), {
            **params, "customer_name": "Sara Ahmed", "customer_phone": "03217654321", "players": 4,
        })
        booking = Booking.objects.get()
        self.assertRedirects(r, reverse("billing:pay", args=[booking.invoice.token]))
        return booking

    def checkout(self, booking, option="deposit"):
        r = self.client.post(reverse("billing:pay", args=[booking.invoice.token]), {"option": option})
        self.assertTemplateUsed(r, "billing/demo_checkout.html")
        return r

    def test_pay_deposit_confirms_booking(self):
        booking = self.start_booking()
        r = self.client.get(reverse("billing:pay", args=[booking.invoice.token]))
        self.assertContains(r, "Pay deposit")
        r = self.checkout(booking)
        ret = reverse("billing:gateway_return", args=["demo"])
        r = self.client.post(ret, {"token": demo_token(r)})
        self.assertRedirects(r, reverse("bookings:detail", args=[booking.reference]))
        booking.refresh_from_db()
        self.assertEqual(booking.status, Booking.Status.CONFIRMED)
        self.assertEqual(booking.invoice.status, Invoice.Status.PARTIAL)
        self.assertEqual(booking.invoice.amount_paid, Decimal("2000"))
        attempt = OnlinePayment.objects.get()
        self.assertEqual(attempt.state, OnlinePayment.State.SUCCEEDED)
        self.assertEqual(booking.invoice.payments.get().reference, attempt.reference)

    def test_callback_replay_is_ignored(self):
        booking = self.start_booking()
        token = demo_token(self.checkout(booking))
        ret = reverse("billing:gateway_return", args=["demo"])
        self.client.post(ret, {"token": token})
        self.client.post(ret, {"token": token})
        self.assertEqual(booking.invoice.payments.count(), 1)

    def test_pay_full_amount(self):
        booking = self.start_booking()
        r = self.checkout(booking, option="full")
        self.client.post(reverse("billing:gateway_return", args=["demo"]), {"token": demo_token(r)})
        booking.invoice.refresh_from_db()
        self.assertEqual(booking.invoice.status, Invoice.Status.PAID)

    def test_declined_payment_keeps_hold(self):
        booking = self.start_booking()
        r = self.checkout(booking)
        r = self.client.post(reverse("billing:gateway_return", args=["demo"]), {"token": demo_token(r, "fail_token")})
        self.assertRedirects(r, reverse("billing:pay", args=[booking.invoice.token]) + "?failed=1")
        booking.refresh_from_db()
        self.assertEqual(booking.status, Booking.Status.AWAITING_PAYMENT)
        self.assertEqual(OnlinePayment.objects.get().state, OnlinePayment.State.FAILED)

    def test_forged_callback_rejected(self):
        booking = self.start_booking()
        self.checkout(booking)
        self.client.post(reverse("billing:gateway_return", args=["demo"]), {"token": "forged"})
        booking.refresh_from_db()
        self.assertEqual(booking.status, Booking.Status.AWAITING_PAYMENT)
        self.assertFalse(booking.invoice.payments.exists())

    def test_cannot_start_payment_after_hold_expired(self):
        booking = self.start_booking()
        Booking.objects.filter(pk=booking.pk).update(payment_due_at=timezone.now() - timedelta(minutes=1))
        r = self.client.post(reverse("billing:pay", args=[booking.invoice.token]), {"option": "deposit"})
        self.assertRedirects(r, reverse("billing:pay", args=[booking.invoice.token]))
        self.assertFalse(OnlinePayment.objects.exists())

    def test_late_payment_revives_free_slot(self):
        booking = self.start_booking()
        token = demo_token(self.checkout(booking))
        services.expire_unpaid_bookings(now=booking.payment_due_at + timedelta(seconds=1))
        self.client.post(reverse("billing:gateway_return", args=["demo"]), {"token": token})
        booking.refresh_from_db()
        self.assertEqual(booking.status, Booking.Status.CONFIRMED)
        self.assertEqual(booking.invoice.status, Invoice.Status.PARTIAL)

    def test_late_payment_after_slot_rebooked_flags_refund(self):
        booking = self.start_booking()
        token = demo_token(self.checkout(booking))
        Booking.objects.filter(pk=booking.pk).update(payment_due_at=timezone.now() - timedelta(seconds=1))
        self.book(staff=True, customer_name="Walk In")  # takes the released slot
        self.client.post(reverse("billing:gateway_return", args=["demo"]), {"token": token})
        booking.refresh_from_db()
        self.assertEqual(booking.status, Booking.Status.EXPIRED)
        self.assertIn("refund due", booking.notes)
        staff = self.member("o@c.pk")
        self.client.force_login(staff)
        r = self.client.get(reverse("dashboard:overview"))
        self.assertContains(r, "refund or credit")
        self.client.post(reverse("dashboard:booking_refund_handled", args=[booking.pk]))
        r = self.client.get(reverse("dashboard:overview"))
        self.assertNotContains(r, "refund or credit")


class JazzCashTests(BaseCase):
    def setUp(self):
        super().setUp()
        self.s.payment_gateway = "jazzcash"
        self.s.jazzcash_merchant_id, self.s.jazzcash_password = "MC12345", "pass123"
        self.s.jazzcash_integrity_salt = "salt123"
        self.s.prepayment_mode = "deposit"
        self.s.save()

    def test_hash_format(self):
        import hashlib
        import hmac

        fields = {"pp_Amount": "25000", "pp_MerchantID": "MC25041", "pp_TxnCurrency": "PKR", "pp_BankID": "",
                  "pp_TxnRefNo": "T1", "pp_SecureHash": "ignored"}
        msg = "salt&25000&MC25041&PKR&T1"  # sorted keys, empty values skipped, SecureHash excluded
        expected = hmac.new(b"salt", msg.encode(), hashlib.sha256).hexdigest().upper()
        self.assertEqual(jazzcash_hash(fields, "salt"), expected)

    def test_checkout_form_and_signed_callback(self):
        b = self.book()
        self.assertEqual(b.status, Booking.Status.AWAITING_PAYMENT)
        r = self.client.post(reverse("billing:pay", args=[b.invoice.token]), {"option": "deposit"})
        fields = r.context["fields"]
        self.assertEqual(fields["pp_Amount"], "200000")  # Rs 2,000 in paisa
        self.assertEqual(fields["pp_SecureHash"], jazzcash_hash(fields, "salt123"))
        self.assertIn("sandbox.jazzcash.com.pk", r.context["action"])

        response = {"pp_TxnRefNo": fields["pp_TxnRefNo"], "pp_Amount": "200000", "pp_MerchantID": "MC12345",
                    "pp_ResponseCode": "000", "pp_ResponseMessage": "Thank you for using JazzCash"}
        tampered = {**response, "pp_Amount": "100", "pp_SecureHash": jazzcash_hash(response, "salt123")}
        self.client.post(reverse("billing:gateway_return", args=["jazzcash"]), tampered)
        b.refresh_from_db()
        self.assertEqual(b.status, Booking.Status.AWAITING_PAYMENT)

        response["pp_SecureHash"] = jazzcash_hash(response, "salt123")
        self.client.post(reverse("billing:gateway_return", args=["jazzcash"]), response)
        b.refresh_from_db()
        self.assertEqual(b.status, Booking.Status.CONFIRMED)
        self.assertEqual(b.invoice.payments.get().method, "jazzcash")

    def test_ipn_is_idempotent_with_return(self):
        b = self.book()
        r = self.client.post(reverse("billing:pay", args=[b.invoice.token]), {"option": "deposit"})
        ref = r.context["fields"]["pp_TxnRefNo"]
        data = {"pp_TxnRefNo": ref, "pp_Amount": "200000", "pp_ResponseCode": "000"}
        data["pp_SecureHash"] = jazzcash_hash(data, "salt123")
        ipn = self.client.post(reverse("billing:jazzcash_ipn"), data)
        self.assertEqual(ipn.json()["pp_ResponseCode"], "000")
        self.client.post(reverse("billing:gateway_return", args=["jazzcash"]), data)
        self.assertEqual(b.invoice.payments.count(), 1)

    def test_failed_response_code(self):
        b = self.book()
        r = self.client.post(reverse("billing:pay", args=[b.invoice.token]), {"option": "deposit"})
        data = {"pp_TxnRefNo": r.context["fields"]["pp_TxnRefNo"], "pp_ResponseCode": "124",
                "pp_ResponseMessage": "Insufficient balance"}
        data["pp_SecureHash"] = jazzcash_hash(data, "salt123")
        self.client.post(reverse("billing:gateway_return", args=["jazzcash"]), data)
        self.assertEqual(OnlinePayment.objects.get().response_message, "Insufficient balance")


def wa_ok(*args, **kwargs):
    return mock.Mock(status_code=200, json=lambda: {"messages": [{"id": "wamid.TEST"}]})


class WhatsAppTests(BaseCase):
    def setUp(self):
        super().setUp()
        self.s.whatsapp_enabled = True
        self.s.whatsapp_phone_number_id, self.s.whatsapp_access_token = "1234567890", "EAAtoken"
        self.s.save()

    @mock.patch("bookings.whatsapp.requests.post", side_effect=wa_ok)
    def test_confirmation_sent_once(self, post):
        with self.captureOnCommitCallbacks(execute=True):
            b = self.book()
        self.assertEqual(post.call_count, 1)
        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["to"], "923001234567")
        self.assertEqual(payload["template"]["name"], "booking_confirmed")
        self.assertIn(b.reference, [p["text"] for p in payload["template"]["components"][0]["parameters"]])
        self.assertIn("/1234567890/messages", post.call_args.args[0])
        send_notification(b.pk, "confirmation")
        self.assertEqual(post.call_count, 1)
        self.assertEqual(Notification.objects.get().provider_id, "wamid.TEST")

    @mock.patch("bookings.whatsapp.requests.post")
    def test_failure_logged_and_retried(self, post):
        post.return_value = mock.Mock(status_code=400, json=lambda: {"error": {"message": "Template not approved"}})
        with self.captureOnCommitCallbacks(execute=True):
            b = self.book()
        note = Notification.objects.get()
        self.assertEqual((note.state, note.error), ("failed", "Template not approved"))
        post.side_effect = wa_ok
        send_notification(b.pk, "confirmation")
        note.refresh_from_db()
        self.assertEqual((note.state, note.attempts), ("sent", 2))

    @override_settings(PAYMENTS_DEMO_ENABLED=True)
    @mock.patch("bookings.whatsapp.requests.post", side_effect=wa_ok)
    def test_no_confirmation_until_paid(self, post):
        self.s.prepayment_mode, self.s.payment_gateway = "deposit", "demo"
        self.s.save()
        with self.captureOnCommitCallbacks(execute=True):
            b = self.book()
        self.assertEqual(post.call_count, 0)
        with self.captureOnCommitCallbacks(execute=True):
            record_payment(b.invoice, amount=b.prepayment_amount)
        self.assertEqual(post.call_count, 1)

    def _booking(self, at, booked_hours_before):
        b = self.book(at=at, staff=True, customer_phone=f"0300{1000000 + Booking.objects.count():07d}")
        Booking.objects.filter(pk=b.pk).update(created_at=b.start_at - timedelta(hours=booked_hours_before))
        b.refresh_from_db()
        return b

    @mock.patch("bookings.whatsapp.requests.post", side_effect=wa_ok)
    def test_reminder_windows(self, post):
        game = self._booking(time(18), booked_hours_before=48)
        last_minute = self._booking(time(20), booked_hours_before=1)
        club = SiteSettings.load()

        day_before = game.start_at - timedelta(hours=20)
        self.assertEqual({(b.pk, k) for b, k in due_reminders(club, day_before)}, {(game.pk, "reminder_24h")})
        two_hours = game.start_at - timedelta(minutes=90)
        due = {(b.pk, k) for b, k in due_reminders(club, two_hours)}
        self.assertIn((game.pk, "reminder_2h"), due)
        self.assertNotIn(last_minute.pk, {pk for pk, _ in due})  # booked 1h before: confirmation is enough

        post.reset_mock()
        self.assertEqual(send_due_reminders(day_before), 1)
        self.assertEqual(send_due_reminders(day_before), 0)  # never twice
        self.assertEqual(send_due_reminders(two_hours), 1)
        self.assertEqual(post.call_args.kwargs["json"]["template"]["name"], "booking_reminder")

    @mock.patch("bookings.whatsapp.requests.post", side_effect=wa_ok)
    def test_cancelled_bookings_get_no_reminder(self, post):
        game = self._booking(time(18), booked_hours_before=48)
        services.cancel_booking(game, staff=True)
        self.assertEqual(send_due_reminders(game.start_at - timedelta(hours=20)), 0)

    @mock.patch("bookings.whatsapp.requests.post", side_effect=wa_ok)
    def test_dashboard_test_message_and_secret_kept(self, post):
        staff = self.member("s@c.pk")
        self.client.force_login(staff)
        r = self.client.post(reverse("dashboard:settings"), {"test_whatsapp": "1", "test_phone": "0300 7654321"})
        self.assertEqual(post.call_args.kwargs["json"]["template"]["name"], "hello_world")
        # Saving settings with the token field blank keeps the stored token
        r = self.client.get(reverse("dashboard:settings"))
        form = r.context["form"]
        data = {}
        for name, field in form.fields.items():
            value = form.initial.get(name, field.initial)
            if isinstance(value, bool):
                if value:
                    data[name] = "on"
            elif value is not None:
                data[name] = value.strftime("%H:%M") if hasattr(value, "strftime") else value
        data["whatsapp_access_token"] = ""
        r = self.client.post(reverse("dashboard:settings"), data)
        self.assertRedirects(r, reverse("dashboard:settings"))
        self.assertEqual(SiteSettings.load().whatsapp_access_token, "EAAtoken")
