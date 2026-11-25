from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core import signing
from django.db import transaction
from django.urls import reverse

from bookings.notifications import normalize_phone

from .context import use_club
from .models import Club, Membership

MAGIC_SALT = "tenants.magic-login"


@transaction.atomic
def create_club(*, club_name, subdomain, owner_name, email, phone, password, city="", existing_user=None):
    """Self-service signup: club + settings + owner login + starter courts, ready to take bookings."""
    from core.models import SiteSettings
    from courts.models import Court

    first, _, last = owner_name.strip().partition(" ")
    user = existing_user or get_user_model().objects.create_user(
        username=email, email=email, password=password, first_name=first, last_name=last, phone=phone,
    )
    club = Club.objects.create(name=club_name, slug=subdomain)
    Membership.objects.create(user=user, club=club, role=Membership.Role.OWNER)
    SiteSettings.objects.create(
        club=club, club_name=club_name, phone=phone, whatsapp=normalize_phone(phone), email=email,
        address=city, tagline="Book your court in under 30 seconds.",
    )
    with use_club(club):
        for i in (1, 2):
            Court.objects.create(name=f"Court {i}", price_off_peak=Decimal("4000"), price_peak=Decimal("5500"),
                                 sort_order=i, features="LED lights")
    return club, user


def magic_login_url(user, club, next_url=None):
    """Short-lived, single-use link that logs `user` into `club`'s site (different host = different cookie)."""
    token = signing.dumps(
        {"u": user.pk, "c": club.pk, "l": user.last_login.isoformat() if user.last_login else ""},
        salt=MAGIC_SALT,
    )
    url = club.base_url + reverse("accounts:magic_login", args=[token], urlconf="config.urls")  # club-site URL
    return url + (f"?next={next_url}" if next_url else "")


def check_magic_token(token, club, max_age=300):
    try:
        data = signing.loads(token, salt=MAGIC_SALT, max_age=max_age)
    except signing.BadSignature:
        return None
    if data.get("c") != club.pk:
        return None
    user = get_user_model().objects.filter(pk=data.get("u"), is_active=True).first()
    if not user or data.get("l") != (user.last_login.isoformat() if user.last_login else ""):
        return None  # already used (logging in updates last_login)
    if not (user.is_superuser or Membership.objects.filter(user=user, club=club).exists()):
        return None
    return user
