import re
from datetime import timedelta

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.db import models
from django.utils import timezone

from .context import get_current_club
from .plans import DEFAULT_PLAN, GRACE_DAYS, PLANS, TRIAL_DAYS

RESERVED_SLUGS = {"www", "admin", "app", "api", "platform", "static", "media", "mail", "dashboard", "help",
                  "support", "billing", "status", "blog"}
SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{1,38}[a-z0-9])$")


def validate_subdomain(value):
    if not SLUG_RE.match(value or ""):
        raise ValidationError("Use 3–40 lowercase letters, numbers or hyphens (e.g. smash-padel).")
    if value in RESERVED_SLUGS:
        raise ValidationError("That address is reserved. Please choose another.")


def default_trial_end():
    return timezone.localdate() + timedelta(days=TRIAL_DAYS)


class Club(models.Model):
    """A customer of the platform: one padel club with its own site, data and staff."""

    PLAN_CHOICES = [(key, plan["label"]) for key, plan in PLANS.items()]

    name = models.CharField(max_length=80)
    slug = models.CharField("Subdomain", max_length=40, unique=True, validators=[validate_subdomain])
    custom_domain = models.CharField(
        max_length=120, unique=True, null=True, blank=True,
        help_text="Optional, e.g. book.smashpadel.pk (point its DNS to the platform)",
    )
    plan = models.CharField(max_length=20, choices=PLAN_CHOICES, default=DEFAULT_PLAN)
    trial_ends_at = models.DateField(default=default_trial_end)
    paid_until = models.DateField(null=True, blank=True)
    is_suspended = models.BooleanField(default=False, help_text="Manually switch the club off")
    notes = models.TextField(blank=True, help_text="Internal notes (platform team only)")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        self.slug = (self.slug or "").strip().lower()
        self.custom_domain = (self.custom_domain or "").strip().lower() or None
        super().save(*args, **kwargs)

    # ---- plan & subscription
    @property
    def plan_info(self):
        return PLANS.get(self.plan, PLANS[DEFAULT_PLAN])

    @property
    def monthly_price(self):
        return self.plan_info["price"]

    @property
    def max_courts(self):
        return self.plan_info["max_courts"]

    @property
    def has_whatsapp(self):
        return self.plan_info["whatsapp"]

    @property
    def covered_until(self):
        """Last day the club is paid up or in trial."""
        return max(d for d in (self.trial_ends_at, self.paid_until) if d)

    @property
    def subscription_state(self):
        today = timezone.localdate()
        if self.is_suspended:
            return "suspended"
        if self.paid_until and self.paid_until >= today:
            return "active"
        if self.trial_ends_at >= today:
            return "trial"
        if self.covered_until + timedelta(days=GRACE_DAYS) >= today:
            return "grace"
        return "expired"

    @property
    def is_operational(self):
        return self.subscription_state in ("active", "trial", "grace")

    @property
    def days_left(self):
        return (self.covered_until - timezone.localdate()).days

    def extend(self, months=1):
        start = max(self.paid_until or timezone.localdate(), timezone.localdate())
        self.paid_until = start + timedelta(days=30 * months)
        self.save(update_fields=["paid_until"])

    # ---- addresses
    @property
    def host(self):
        return self.custom_domain or f"{self.slug}.{settings.PLATFORM_DOMAIN}"

    @property
    def base_url(self):
        port = "" if self.custom_domain else settings.PLATFORM_PORT
        return f"{settings.PLATFORM_SCHEME}://{self.host}{port}"


class Membership(models.Model):
    class Role(models.TextChoices):
        OWNER = "owner", "Owner"
        STAFF = "staff", "Staff (reception)"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="memberships")
    club = models.ForeignKey(Club, on_delete=models.CASCADE, related_name="memberships")
    role = models.CharField(max_length=10, choices=Role.choices, default=Role.STAFF)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "club"], name="one_membership_per_club")]

    def __str__(self):
        return f"{self.user} @ {self.club} ({self.role})"


# --------------------------------------------------------------------------- scoping for club-owned models
class TenantManager(models.Manager):
    """Filters every query to the active club. With no active club (platform admin, migrations)
    it returns all rows — so platform code must never run inside a club request without one."""

    def get_queryset(self):
        qs = super().get_queryset()
        club = get_current_club()
        return qs.filter(club=club) if club is not None else qs


class TenantModel(models.Model):
    club = models.ForeignKey(Club, on_delete=models.CASCADE, related_name="+")

    objects = TenantManager()
    all_clubs = models.Manager()  # explicit, unscoped access for platform-level code

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if not self.club_id:
            club = get_current_club()
            if club is None:
                raise ImproperlyConfigured(f"Can't save {type(self).__name__} without an active club.")
            self.club = club
        super().save(*args, **kwargs)
