from datetime import time, timedelta
from decimal import Decimal
from io import StringIO
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from billing.models import Invoice
from bookings import services
from bookings.models import Booking
from bookings.notifications import channels_for
from core.models import SiteSettings
from courts.models import Court

from .context import use_club
from .models import Club, Membership
from .services import magic_login_url

PLATFORM = "courtpro.pk"


def next_monday():
    d = timezone.localdate() + timedelta(days=2)
    while d.weekday() != 0:
        d += timedelta(days=1)
    return d


@override_settings(PLATFORM_DOMAIN=PLATFORM, ALLOWED_HOSTS=["*"], PLATFORM_SCHEME="https", PLATFORM_PORT="")
class TenantCase(TestCase):
    def setUp(self):
        paid = timezone.localdate() + timedelta(days=30)
        self.a = Club.objects.create(name="Smash", slug="smash", paid_until=paid)
        self.b = Club.objects.create(name="Baseline", slug="baseline", custom_domain="book.baseline.pk",
                                     paid_until=paid)
        self.day = next_monday()
        self.users = {}

    def court(self, club, name="Court 1", **kw):
        with use_club(club):
            return Court.objects.create(name=name, price_off_peak=Decimal("4000"), price_peak=Decimal("6000"), **kw)

    def booking(self, club, court, at=time(10), **kw):
        with use_club(club):
            s = SiteSettings.load(club)
            return services.create_booking(
                court=court, business_date=self.day, start_at=services.combine(self.day, at, s), duration=60,
                customer_name="Ali", customer_phone="03001234567", staff=True, **kw)

    def member(self, club, email, role="owner"):
        user = get_user_model().objects.filter(username=email).first() or \
            get_user_model().objects.create_user(email, email, "pass12345")
        Membership.objects.create(user=user, club=club, role=role)
        return user

    def get(self, host, path, **kw):
        return self.client.get(path, HTTP_HOST=host, **kw)

    def post(self, host, path, data=None, **kw):
        return self.client.post(path, data or {}, HTTP_HOST=host, **kw)


class IsolationTests(TenantCase):
    def test_queries_only_see_the_active_club(self):
        ca, cb = self.court(self.a), self.court(self.b)
        self.booking(self.a, ca)
        self.booking(self.b, cb)
        with use_club(self.a):
            self.assertEqual(list(Court.objects.all()), [ca])
            self.assertEqual(Booking.objects.count(), 1)
            self.assertEqual(Invoice.objects.count(), 1)
        self.assertEqual(Booking.all_clubs.count(), 2)

    def test_same_court_slug_and_invoice_numbers_per_club(self):
        ca, cb = self.court(self.a), self.court(self.b)
        self.assertEqual(ca.slug, cb.slug)
        ia, ib = self.booking(self.a, ca).invoice, self.booking(self.b, cb).invoice
        self.assertEqual(ia.number, ib.number)  # each club has its own INV-YYYY-00001

    def test_saving_without_a_club_is_refused(self):
        with self.assertRaises(ImproperlyConfigured):
            Court.objects.create(name="Orphan", price_off_peak=1, price_peak=1)

    def test_court_overlap_is_per_club(self):
        ca = self.court(self.a)
        self.booking(self.a, ca)
        cb = self.court(self.b)
        self.booking(self.b, cb)  # same time, other club: fine

    def test_web_pages_never_leak_across_clubs(self):
        ca, cb = self.court(self.a, name="Smash Court"), self.court(self.b, name="Baseline Court")
        ba = self.booking(self.a, ca)
        r = self.get(f"baseline.{PLATFORM}", f"/book/?date={self.day.isoformat()}")
        self.assertContains(r, "Baseline Court")
        self.assertNotContains(r, "Smash Court")
        self.assertEqual(self.get("book.baseline.pk", f"/invoice/{ba.invoice.token}/").status_code, 404)
        self.assertEqual(self.get(f"smash.{PLATFORM}", f"/invoice/{ba.invoice.token}/").status_code, 200)
        r = self.post("book.baseline.pk", "/manage-booking/", {"reference": ba.reference, "phone": "03001234567"})
        self.assertContains(r, "couldn")

    def test_staff_of_one_club_cannot_open_another(self):
        owner_a = self.member(self.a, "owner@smash.pk")
        self.client.force_login(owner_a)
        self.assertEqual(self.get(f"smash.{PLATFORM}", "/dashboard/").status_code, 200)
        self.assertEqual(self.get(f"baseline.{PLATFORM}", "/dashboard/").status_code, 302)


class HostRoutingTests(TenantCase):
    def test_hosts(self):
        self.assertContains(self.get(PLATFORM, "/"), "Start")  # platform landing
        self.assertContains(self.get(f"www.{PLATFORM}", "/"), "Start")
        self.assertContains(self.get(f"smash.{PLATFORM}", "/"), "Smash")
        self.assertContains(self.get("book.baseline.pk", "/"), "Baseline")
        self.assertEqual(self.get(f"nope.{PLATFORM}", "/").status_code, 404)
        self.assertEqual(self.get("random.example.com", "/").status_code, 404)

    def test_platform_urls_are_not_on_club_hosts(self):
        self.assertEqual(self.get(f"smash.{PLATFORM}", "/platform/").status_code, 404)
        self.assertEqual(self.get(PLATFORM, "/dashboard/").status_code, 404)

    def test_links_use_the_clubs_own_address(self):
        self.assertEqual(self.a.base_url, f"https://smash.{PLATFORM}")
        self.assertEqual(self.b.base_url, "https://book.baseline.pk")


class SignupTests(TenantCase):
    data = {"club_name": "Rally Padel", "subdomain": "rally", "city": "Islamabad", "owner_name": "Sana Khan",
            "email": "sana@rally.pk", "phone": "0321 1234567", "password": "a-strong-pass-123"}

    def test_signup_creates_a_ready_club_and_logs_in(self):
        r = self.post(PLATFORM, "/start/", self.data)
        club = Club.objects.get(slug="rally")
        self.assertTrue(r["Location"].startswith(f"https://rally.{PLATFORM}/accounts/magic/"))
        self.assertEqual(club.subscription_state, "trial")
        self.assertTrue(Membership.objects.filter(club=club, role="owner", user__username="sana@rally.pk").exists())
        self.assertEqual(SiteSettings.load(club).whatsapp, "923211234567")
        self.assertEqual(Court.all_clubs.filter(club=club).count(), 2)

        path = r["Location"].split(PLATFORM, 1)[1]
        r = self.get(f"rally.{PLATFORM}", path)
        self.assertEqual(r["Location"], "/dashboard/?welcome=1")
        self.assertContains(self.get(f"rally.{PLATFORM}", "/dashboard/?welcome=1"), "Welcome")
        self.client.logout()
        r = self.get(f"rally.{PLATFORM}", path)  # single use
        self.assertRedirects(r, "/accounts/login/", fetch_redirect_response=False)

    def test_taken_and_reserved_subdomains(self):
        for slug in ("smash", "admin", "a", "Bad_Name"):
            r = self.post(PLATFORM, "/start/", {**self.data, "subdomain": slug})
            self.assertEqual(r.status_code, 200, slug)
        self.assertFalse(Club.objects.filter(name="Rally Padel").exists())

    def test_magic_link_only_works_on_its_own_club(self):
        owner = self.member(self.a, "o@smash.pk")
        url = magic_login_url(owner, self.a)
        path = url.split(PLATFORM, 1)[1]
        r = self.get(f"baseline.{PLATFORM}", path)
        self.assertRedirects(r, "/accounts/login/", fetch_redirect_response=False)


class RoleTests(TenantCase):
    def setUp(self):
        super().setUp()
        self.host = f"smash.{PLATFORM}"
        self.owner = self.member(self.a, "owner@smash.pk", "owner")
        self.staff = self.member(self.a, "desk@smash.pk", "staff")
        ca = self.court(self.a)
        self.inv = self.booking(self.a, ca).invoice

    def test_staff_runs_the_day_but_not_the_business(self):
        self.client.force_login(self.staff)
        for path in ("/dashboard/", "/dashboard/schedule/", "/dashboard/bookings/", "/dashboard/invoices/",
                     "/dashboard/customers/"):
            self.assertEqual(self.get(self.host, path).status_code, 200, path)
        for path in ("/dashboard/settings/", "/dashboard/courts/", "/dashboard/promos/", "/dashboard/team/",
                     "/dashboard/bookings/export.csv"):
            self.assertEqual(self.get(self.host, path).status_code, 403, path)
        self.assertEqual(self.post(self.host, f"/dashboard/invoices/{self.inv.pk}/void/").status_code, 403)

    def test_owner_manages_team(self):
        self.client.force_login(self.owner)
        r = self.post(self.host, "/dashboard/team/", {"first_name": "Bilal", "email": "Bilal@Smash.pk",
                                                       "role": "staff", "password": "another-pass-456"})
        self.assertEqual(r.status_code, 302)
        m = Membership.objects.get(user__username="bilal@smash.pk")
        self.assertTrue(self.client.login(username="bilal@smash.pk", password="another-pass-456"))
        self.client.force_login(self.owner)
        self.post(self.host, f"/dashboard/team/{m.pk}/remove/")
        self.assertFalse(Membership.objects.filter(pk=m.pk).exists())

    def test_last_owner_cannot_be_removed(self):
        self.client.force_login(self.owner)
        me = Membership.objects.get(user=self.owner)
        self.post(self.host, f"/dashboard/team/{me.pk}/remove/")
        self.assertTrue(Membership.objects.filter(pk=me.pk).exists())


class PlanAndSubscriptionTests(TenantCase):
    def test_states(self):
        today = timezone.localdate()
        c = Club(name="x", slug="xx", trial_ends_at=today + timedelta(days=3))
        self.assertEqual(c.subscription_state, "trial")
        c.trial_ends_at = today - timedelta(days=2)
        self.assertEqual(c.subscription_state, "grace")
        c.trial_ends_at = today - timedelta(days=30)
        self.assertEqual(c.subscription_state, "expired")
        c.paid_until = today
        self.assertEqual(c.subscription_state, "active")
        c.is_suspended = True
        self.assertEqual(c.subscription_state, "suspended")

    def test_expired_club_stops_online_booking_and_dashboard(self):
        self.a.paid_until = None
        self.a.trial_ends_at = timezone.localdate() - timedelta(days=60)
        self.a.save()
        self.court(self.a)
        host = f"smash.{PLATFORM}"
        r = self.get(host, "/book/")
        self.assertEqual(r.status_code, 503)
        self.assertContains(r, "paused", status_code=503)
        self.client.force_login(self.member(self.a, "o@smash.pk"))
        self.assertRedirects(self.get(host, "/dashboard/"), "/dashboard/subscription/", fetch_redirect_response=False)
        self.assertContains(self.get(host, "/dashboard/subscription/"), "How to pay")

    def test_starter_court_limit(self):
        self.a.plan = "starter"
        self.a.save()
        self.court(self.a, "C1")
        self.court(self.a, "C2")
        self.client.force_login(self.member(self.a, "o@smash.pk"))
        r = self.get(f"smash.{PLATFORM}", "/dashboard/courts/new/")
        self.assertRedirects(r, "/dashboard/subscription/", fetch_redirect_response=False)

    def test_whatsapp_is_a_pro_feature(self):
        s = SiteSettings.load(self.a)
        s.whatsapp_enabled, s.whatsapp_phone_number_id, s.whatsapp_access_token = True, "1", "t"
        s.save()
        b = self.booking(self.a, self.court(self.a))
        self.assertIn("whatsapp", channels_for(b, s))
        self.a.plan = "starter"
        b.club = self.a
        self.assertNotIn("whatsapp", channels_for(b, s))


class PlatformAdminTests(TenantCase):
    def setUp(self):
        super().setUp()
        self.admin = get_user_model().objects.create_superuser("root@cp.pk", "root@cp.pk", "pass12345")

    def test_only_superusers(self):
        self.assertEqual(self.get(PLATFORM, "/platform/").status_code, 302)
        self.client.force_login(self.member(self.a, "o@smash.pk"))
        self.assertEqual(self.get(PLATFORM, "/platform/").status_code, 302)

    def test_dashboard_and_actions(self):
        self.client.force_login(self.admin)
        r = self.get(PLATFORM, "/platform/")
        self.assertContains(r, "Smash")
        self.assertContains(r, "Baseline")
        self.assertContains(r, "Rs 30,000")  # MRR: two active Pro clubs
        before = self.a.paid_until
        self.post(PLATFORM, f"/platform/clubs/{self.a.pk}/action/", {"action": "extend", "months": 1})
        self.a.refresh_from_db()
        self.assertEqual(self.a.paid_until, before + timedelta(days=30))
        self.post(PLATFORM, f"/platform/clubs/{self.a.pk}/action/", {"action": "suspend"})
        self.a.refresh_from_db()
        self.assertTrue(self.a.is_suspended)
        r = self.post(PLATFORM, f"/platform/clubs/{self.b.pk}/action/", {"action": "open"})
        self.assertTrue(r["Location"].startswith("https://book.baseline.pk/accounts/magic/"))


class ScheduledJobTests(TenantCase):
    @override_settings(PAYMENTS_DEMO_ENABLED=True)
    def test_jobs_run_for_every_club(self):
        for club in (self.a, self.b):
            s = SiteSettings.load(club)
            s.prepayment_mode, s.payment_gateway = "deposit", "demo"
            s.save()
            court = self.court(club)
            with use_club(club):
                b = services.create_booking(
                    court=court, business_date=self.day, start_at=services.combine(self.day, time(10), s),
                    duration=60, customer_name="Ali", customer_phone="03001234567")
                Booking.objects.filter(pk=b.pk).update(payment_due_at=timezone.now() - timedelta(minutes=1))
        out = StringIO()
        call_command("run_scheduled_jobs", stdout=out)
        self.assertEqual(Booking.all_clubs.filter(status="expired").count(), 2)
        self.assertIn("[smash] expired=1", out.getvalue())
        self.assertIn("[baseline] expired=1", out.getvalue())


class LandingTests(TenantCase):
    def test_landing_shows_plans_demo_and_finder(self):
        with self.settings(PLATFORM_DEMO_CLUB="smash"):
            r = self.get(PLATFORM, "/")
        self.assertContains(r, "Every court booked.")
        self.assertContains(r, f"https://smash.{PLATFORM}")  # demo club link
        self.assertContains(r, "Rs 15,000")
        self.assertContains(r, "Questions owners ask")

    def test_club_login_finder(self):
        r = self.get(PLATFORM, "/club-login/?club=Smash")
        self.assertRedirects(r, f"https://smash.{PLATFORM}/accounts/login/", fetch_redirect_response=False)
        r = self.get(PLATFORM, f"/club-login/?club=https://baseline.{PLATFORM}/")
        self.assertRedirects(r, "https://book.baseline.pk/accounts/login/", fetch_redirect_response=False)
        r = self.get(PLATFORM, "/club-login/?club=book.baseline.pk")
        self.assertEqual(r.status_code, 302)
        r = self.get(PLATFORM, "/club-login/?club=nope")
        self.assertContains(r, "couldn")
