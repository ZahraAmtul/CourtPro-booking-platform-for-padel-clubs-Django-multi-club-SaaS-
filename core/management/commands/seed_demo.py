"""Load realistic demo data: courts, extras, promo codes, an owner account and bookings.

    python manage.py seed_demo            # two demo clubs + platform admin
    python manage.py seed_demo --reset    # wipes bookings/invoices first
"""
import random
from datetime import time, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from billing import reports, wallets
from billing.models import CreditPackage, DayClose, Invoice, InvoiceSequence, Wallet
from billing.services import record_payment
from bookings import services
from bookings.models import Booking, BookingSeries, Extra, PromoCode
from core.models import SiteSettings
from courts.models import Court
from tenants.context import use_club
from tenants.models import Club, Membership

COURTS = [
    ("Court 1 — Panoramic", "panoramic", "Full glass walls, perfect for spectators.", "Panoramic glass, LED lights, WPT turf", 5000, 6500),
    ("Court 2 — Indoor", "indoor", "Climate-controlled — play any weather.", "Air conditioned, LED lights", 4500, 6000),
    ("Court 3 — Indoor", "indoor", "Our most popular court for league nights.", "Air conditioned, Pro turf", 4500, 6000),
    ("Court 4 — Outdoor", "outdoor", "Great value for morning and evening games.", "Floodlights, Shaded seating", 3500, 4500),
]
EXTRAS = [
    ("Racket rental", "Bullpadel / Head rackets", 500, 4),
    ("Ball tube (3 balls)", "New pressurised balls", 1200, 3),
    ("Coach (per session)", "Certified coach on court", 3000, 1),
    ("Mineral water", "500 ml", 100, 8),
]
NAMES = ["Ali Raza", "Hamza Khan", "Sara Ahmed", "Usman Tariq", "Ayesha Malik", "Bilal Sheikh", "Fatima Noor",
         "Zain Abbas", "Hira Javed", "Omar Farooq", "Mahnoor Iqbal", "Saad Qureshi"]


class Command(BaseCommand):
    help = "Seed demo data for the padel club"

    def add_arguments(self, parser):
        parser.add_argument("--reset", action="store_true", help="Delete existing bookings & invoices first")

    @transaction.atomic
    def handle(self, *args, **opts):
        random.seed(7)
        User = get_user_model()

        def user(email, password, first, superuser=False):
            u = User.objects.filter(username=email).first()
            if not u:
                make = User.objects.create_superuser if superuser else User.objects.create_user
                u = make(email, email, password, first_name=first, phone="03001234567")
                self.stdout.write(self.style.SUCCESS(f"  login: {email} / {password}"))
            return u

        self.stdout.write("Platform admin (log in at the bare domain, e.g. http://localhost:8000/login/):")
        user("admin@courtpro.test", "admin12345", "Platform", superuser=True)

        clubs = [
            ("smash", "Smash Padel Club", "pro", "Plot 12, DHA Phase 6, Lahore", COURTS, 20),
            ("baseline", "Baseline Padel", "starter", "Block 5, Clifton, Karachi", COURTS[1:3], None),
        ]
        for slug, name, plan, address, courts_spec, paid_days in clubs:
            club, created = Club.objects.get_or_create(slug=slug, defaults={"name": name, "plan": plan})
            if paid_days:
                club.paid_until = timezone.localdate() + timedelta(days=paid_days)
                club.save(update_fields=["paid_until"])
            self.stdout.write(f"{name}: {club.base_url}")
            owner = user(f"owner@{slug}.test", "owner12345", "Junaid" if slug == "smash" else "Sana")
            Membership.objects.get_or_create(user=owner, club=club, defaults={"role": "owner"})
            if slug == "smash":
                staff = user("staff@smash.test", "staff12345", "Reception")
                Membership.objects.get_or_create(user=staff, club=club, defaults={"role": "staff"})
            with use_club(club):
                self.seed_club(club, name, address, courts_spec, owner, opts["reset"])

    def seed_club(self, club, name, address, courts_spec, owner, reset):
        s = SiteSettings.load(club)
        s.club_name, s.address = name, address
        s.prepayment_mode, s.deposit_percent, s.payment_gateway = "deposit", 50, "demo"
        s.save()
        if reset:
            Invoice.objects.all().delete()
            Booking.objects.all().delete()
            InvoiceSequence.objects.all().delete()

        for i, (cname, kind, desc, feats, off, peak) in enumerate(courts_spec):
            Court.objects.update_or_create(name=cname, defaults=dict(
                court_type=kind, description=desc, features=feats, price_off_peak=off, price_peak=peak, sort_order=i))
        for i, (ename, desc, price, max_q) in enumerate(EXTRAS):
            Extra.objects.update_or_create(name=ename, defaults=dict(
                description=desc, price=price, max_quantity=max_q, sort_order=i))
        PromoCode.objects.update_or_create(code="WELCOME10", defaults=dict(
            description="10% off your first game", discount_type="percent", value=10))
        PromoCode.objects.update_or_create(code="MORNING500", defaults=dict(
            description="Rs 500 off morning games", discount_type="fixed", value=500, min_amount=3000))

        courts = list(Court.objects.filter(is_active=True))
        extras = list(Extra.objects.all())
        today = services.current_business_date(s)
        made = 0
        for offset in range(-12, 5):
            day = today + timedelta(days=offset)
            for court in courts:
                for start_hour in random.sample([8, 10, 12, 15, 17, 18, 19, 20, 21, 22, 23], k=random.randint(2, 6)):
                    start_at = services.combine(day, time(start_hour, 0), s)
                    duration = random.choice([60, 90, 90, 120])
                    if Booking.objects.overlapping(court, start_at, start_at + timedelta(minutes=duration)).exists():
                        continue
                    cust = random.choice(NAMES)
                    phone = f"0300{1234500 + NAMES.index(cust) * 7:07d}"
                    try:
                        booking = services.create_booking(
                            court=court, business_date=day, start_at=start_at, duration=duration,
                            customer_name=cust, customer_phone=phone, customer_email="",
                            extras=[(random.choice(extras), 2)] if random.random() < .4 else [],
                            source=random.choice(["online", "online", "walk_in", "phone"]),
                            staff=True, created_by=owner,
                        )
                    except Exception:
                        continue
                    made += 1
                    if booking.end_at < timezone.now():
                        Booking.objects.filter(pk=booking.pk).update(
                            status=random.choice(["completed"] * 9 + ["no_show"]))
                        if random.random() < .9:
                            p = record_payment(booking.invoice, amount=booking.invoice.balance,
                                               method=random.choice(["cash", "card", "jazzcash", "easypaisa"]),
                                               received_by=owner)
                            p.paid_at = booking.start_at
                            p.save(update_fields=["paid_at"])
                    elif random.random() < .3:
                        record_payment(booking.invoice, amount=booking.invoice.balance, method="online",
                                       received_by=owner)
        holds = list(Booking.objects.filter(business_date__gt=today, source="online", invoice__amount_paid=0)
                     .values_list("pk", flat=True)[2:4])
        for b in Booking.objects.filter(pk__in=holds).select_related("invoice"):
            b.status, b.prepayment_amount = "awaiting_payment", services.prepayment_for(b.invoice.total, s)
            b.payment_due_at = timezone.now() + timedelta(minutes=12)
            b.save()
        pending = list(Booking.objects.filter(business_date__gt=today, source="online").values_list("pk", flat=True)[:2])
        Booking.objects.filter(pk__in=pending).update(status="pending")

        # Credit packages, a few customers with credit, a weekly group and past cash counts
        silver, _ = CreditPackage.objects.update_or_create(name="Silver", defaults=dict(
            price=20000, credit=21000, description="5% bonus credit", sort_order=1))
        gold, _ = CreditPackage.objects.update_or_create(name="Gold", defaults=dict(
            price=45000, credit=50000, description="Best value — 11% bonus", sort_order=2))
        if not Wallet.objects.exists():
            for i, cust in enumerate(NAMES[:4]):
                phone = f"0300{1234500 + NAMES.index(cust) * 7:07d}"
                wallets.sell_credit(phone=phone, name=cust, method="cash" if i % 2 else "jazzcash",
                                    received_by=owner, package=gold if i == 0 else silver)
        if not BookingSeries.objects.exists() and courts:
            first = today + timedelta(days=(1 - today.weekday()) % 7 or 7)  # next Tuesday
            services.create_series(court=courts[0], first_date=first, start_time=time(9, 0), duration=90, weeks=8,
                                   customer_name="Tuesday Morning Group", customer_phone="03004445566",
                                   created_by=owner)
        for back in range(2, 8):
            day = today - timedelta(days=back)
            if not DayClose.objects.filter(business_date=day).exists():
                expected = reports.day_summary(day)["expected_cash"]
                reports.close_day(day, counted_cash=expected - (500 if back == 4 else 0), user=owner,
                                  note="Short by 500 — change given wrongly" if back == 4 else "")
        self.stdout.write(self.style.SUCCESS(f"  seeded {len(courts)} courts, {len(extras)} extras, {made} bookings"))
