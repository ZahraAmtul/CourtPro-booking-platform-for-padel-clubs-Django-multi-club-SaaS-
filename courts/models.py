from decimal import Decimal

from django.db import models
from django.urls import reverse
from django.utils.text import slugify

from tenants.models import TenantModel


class Court(TenantModel):
    class CourtType(models.TextChoices):
        INDOOR = "indoor", "Indoor"
        OUTDOOR = "outdoor", "Outdoor"
        PANORAMIC = "panoramic", "Panoramic"

    name = models.CharField(max_length=60)
    slug = models.SlugField(blank=True)
    court_type = models.CharField(max_length=20, choices=CourtType.choices, default=CourtType.INDOOR)
    description = models.TextField(blank=True)
    features = models.CharField(
        max_length=200, blank=True, help_text="Comma separated, e.g. LED lights, Panoramic glass, AC"
    )
    image = models.ImageField(upload_to="courts/", blank=True)
    price_off_peak = models.DecimalField("Off-peak price / hour", max_digits=10, decimal_places=2)
    price_peak = models.DecimalField("Peak price / hour", max_digits=10, decimal_places=2)
    is_active = models.BooleanField(default=True, help_text="Inactive courts can't be booked (e.g. maintenance)")
    sort_order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "name"]
        constraints = [models.UniqueConstraint(fields=["club", "slug"], name="court_slug_per_club")]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            base = slugify(self.name) or "court"
            slug, n = base, 2
            while Court.objects.filter(slug=slug).exclude(pk=self.pk).exists():
                slug, n = f"{base}-{n}", n + 1
            self.slug = slug
        super().save(*args, **kwargs)

    def get_absolute_url(self):
        return reverse("bookings:book") + f"?court={self.slug}"

    def hourly_rate(self, peak: bool) -> Decimal:
        return self.price_peak if peak else self.price_off_peak

    @property
    def feature_list(self):
        return [f.strip() for f in self.features.split(",") if f.strip()]
