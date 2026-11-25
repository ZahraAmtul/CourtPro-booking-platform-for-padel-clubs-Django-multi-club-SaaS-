from django.conf import settings
from django.shortcuts import render

from .context import reset_current_club, set_current_club
from .models import Club, Membership


def resolve_club(host: str):
    """Return (club, is_platform) for a request host."""
    host = host.split(":")[0].lower().rstrip(".")
    base = settings.PLATFORM_DOMAIN
    if host in {base, f"www.{base}"} | set(settings.PLATFORM_EXTRA_HOSTS):
        return None, True
    if host.endswith("." + base):
        return Club.objects.filter(slug=host[: -len(base) - 1]).first(), False
    return Club.objects.filter(custom_domain=host).first(), False


class TenantMiddleware:
    """Works out which club a request is for from its host name:
    smash.<platform-domain>  -> club with subdomain "smash"
    book.smashpadel.pk       -> club with that custom domain
    <platform-domain>        -> the platform itself (landing page, signup, platform admin)
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        club, is_platform = resolve_club(request.get_host())
        request.club, request.is_platform, request.club_role = club, is_platform, None
        if is_platform:
            request.urlconf = "config.urls_platform"
        elif club is None:
            return render(request, "platform/club_not_found.html", status=404)
        token = set_current_club(club)
        try:
            user = getattr(request, "user", None)
            if club and user is not None and user.is_authenticated:
                if user.is_superuser:
                    request.club_role = Membership.Role.OWNER
                else:
                    request.club_role = (
                        Membership.objects.filter(user=user, club=club).values_list("role", flat=True).first()
                    )
            return self.get_response(request)
        finally:
            reset_current_club(token)
