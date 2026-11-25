from datetime import timedelta

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import user_passes_test
from django.contrib.auth.views import LoginView
from django.db.models import Count, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from accounts.forms import EmailLoginForm

from .forms import ClubAdminForm, ClubSignupForm
from .models import Club
from .plans import PLANS, TRIAL_DAYS
from .services import create_club, magic_login_url

superuser_required = user_passes_test(lambda u: u.is_active and u.is_superuser, login_url="platform_login")


def landing(request):
    demo = Club.objects.filter(slug=settings.PLATFORM_DEMO_CLUB, is_suspended=False).first()
    return render(request, "platform/landing.html", {
        "plans": PLANS, "trial_days": TRIAL_DAYS, "demo_url": demo.base_url if demo else "",
        "support_whatsapp": settings.PLATFORM_SUPPORT_WHATSAPP, "domain": settings.PLATFORM_DOMAIN,
        "pro_price": PLANS["pro"]["price"],
    })


def club_login(request):
    """'Log in to your club': owners type their club address and land on their own login page."""
    raw = request.GET.get("club", "").strip().lower()
    slug = raw.replace("https://", "").replace("http://", "").split("/")[0]
    slug = slug.removesuffix("." + settings.PLATFORM_DOMAIN)
    club = Club.objects.filter(slug=slug).first() or Club.objects.filter(custom_domain=slug).first()
    if not club:
        if raw:
            messages.error(request, f"We couldn't find a club at “{raw}”. Check the address in your welcome email.")
        return render(request, "platform/club_login.html", {"domain": settings.PLATFORM_DOMAIN, "value": raw})
    return redirect(club.base_url + "/accounts/login/")


def signup(request):
    form = ClubSignupForm(request.POST or None, initial={"plan": request.GET.get("plan")})
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        club, user = create_club(
            club_name=data["club_name"], subdomain=data["subdomain"], owner_name=data["owner_name"],
            email=data["email"], phone=data["phone"], password=data["password"], city=data["city"],
            existing_user=form.existing_user,
        )
        return redirect(magic_login_url(user, club, next_url="/dashboard/?welcome=1"))
    return render(request, "platform/signup.html", {
        "form": form, "trial_days": TRIAL_DAYS, "domain": settings.PLATFORM_DOMAIN,
    })


class PlatformLoginView(LoginView):
    form_class = EmailLoginForm
    template_name = "platform/login.html"
    redirect_authenticated_user = True

    def get_default_redirect_url(self):
        return "/platform/"


@superuser_required
def platform_admin(request):
    from billing.models import Payment
    from bookings.models import Booking
    from courts.models import Court

    since = timezone.now() - timedelta(days=30)
    bookings = dict(Booking.all_clubs.filter(created_at__gte=since).values_list("club").annotate(n=Count("id")))
    processed = dict(Payment.all_clubs.filter(paid_at__gte=since, method__in=Payment.CASH_IN_METHODS)
                     .values_list("club").annotate(s=Sum("amount")))
    courts = dict(Court.all_clubs.values_list("club").annotate(n=Count("id")))
    clubs = list(Club.objects.all().order_by("-created_at"))
    for club in clubs:
        club.stats = {"bookings": bookings.get(club.pk, 0), "processed": processed.get(club.pk, 0),
                      "courts": courts.get(club.pk, 0)}
    states = [c.subscription_state for c in clubs]
    return render(request, "platform/admin.html", {
        "clubs": clubs,
        "mrr": sum(c.monthly_price for c in clubs if c.subscription_state == "active"),
        "counts": {s: states.count(s) for s in ("active", "trial", "grace", "expired", "suspended")},
        "bookings_30d": sum(bookings.values()),
        "processed_30d": sum(processed.values()),
    })


@superuser_required
def platform_club(request, pk):
    club = get_object_or_404(Club, pk=pk)
    form = ClubAdminForm(request.POST or None, instance=club)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, f"{club.name} updated.")
        return redirect("platform_admin")
    return render(request, "platform/club_form.html", {"form": form, "club": club})


@superuser_required
@require_POST
def platform_club_action(request, pk):
    club = get_object_or_404(Club, pk=pk)
    action = request.POST.get("action")
    if action == "extend":
        club.extend(months=int(request.POST.get("months", 1)))
        messages.success(request, f"{club.name} is paid until {club.paid_until:%d %b %Y}.")
    elif action == "suspend":
        club.is_suspended = not club.is_suspended
        club.save(update_fields=["is_suspended"])
        messages.success(request, f"{club.name} {'suspended' if club.is_suspended else 're-activated'}.")
    elif action == "open":
        return redirect(magic_login_url(request.user, club, next_url="/dashboard/"))
    return redirect("platform_admin")
