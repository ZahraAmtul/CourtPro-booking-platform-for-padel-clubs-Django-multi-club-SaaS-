from django.conf import settings

from .models import SiteSettings


def club(request):
    tenant = getattr(request, "club", None)
    base = {"platform_name": settings.PLATFORM_NAME, "tenant": tenant}
    if tenant is None:
        return base
    return {**base, "club": SiteSettings.load(tenant), "club_role": getattr(request, "club_role", None)}
