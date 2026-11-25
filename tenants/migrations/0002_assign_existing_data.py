"""Move data created before multi-club support into one club, so upgrades keep working."""
from django.db import migrations
from django.utils.text import slugify

CLUB_OWNED = [
    ("courts", "Court"), ("bookings", "Extra"), ("bookings", "PromoCode"), ("bookings", "Booking"),
    ("billing", "InvoiceSequence"), ("billing", "Invoice"), ("billing", "Payment"), ("billing", "OnlinePayment"),
]


def forwards(apps, schema_editor):
    Club = apps.get_model("tenants", "Club")
    Membership = apps.get_model("tenants", "Membership")
    SiteSettings = apps.get_model("core", "SiteSettings")
    User = apps.get_model("accounts", "User")
    has_data = SiteSettings.objects.exists() or any(
        apps.get_model(app, model).objects.exists() for app, model in CLUB_OWNED
    )
    if not has_data:
        return
    settings_row = SiteSettings.objects.order_by("pk").first()
    name = settings_row.club_name if settings_row else "My Padel Club"
    club = Club.objects.create(name=name, slug=(slugify(name) or "club")[:40].strip("-"))
    SiteSettings.objects.filter(club__isnull=True).update(club=club)
    for app, model in CLUB_OWNED:
        apps.get_model(app, model).objects.filter(club__isnull=True).update(club=club)
    for user in User.objects.filter(is_staff=True):  # existing dashboard users become owners
        Membership.objects.get_or_create(user=user, club=club, defaults={"role": "owner"})


class Migration(migrations.Migration):
    dependencies = [
        ("tenants", "0001_initial"),
        ("accounts", "0001_initial"),
        ("core", "0003_sitesettings_club"),
        ("courts", "0002_court_club_alter_court_slug_and_more"),
        ("bookings", "0003_booking_club_extra_club_promocode_club_and_more"),
        ("billing", "0003_invoice_club_invoicesequence_club_onlinepayment_club_and_more"),
    ]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
