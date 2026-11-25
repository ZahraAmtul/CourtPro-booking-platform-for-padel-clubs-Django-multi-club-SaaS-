from django.contrib import messages
from django.contrib.auth import login
from django.utils.http import url_has_allowed_host_and_scheme
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LoginView
from django.db.models import Q
from django.shortcuts import redirect, render
from django.utils import timezone

from bookings.models import Booking

from .forms import EmailLoginForm, ProfileForm, SignUpForm


class EmailLoginView(LoginView):
    form_class = EmailLoginForm
    template_name = "accounts/login.html"
    redirect_authenticated_user = True


def signup(request):
    if request.user.is_authenticated:
        return redirect("accounts:my_bookings")
    form = SignUpForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = form.save()
        login(request, user)
        messages.success(request, "Welcome! Your account is ready.")
        return redirect(request.GET.get("next") or "bookings:book")
    return render(request, "accounts/signup.html", {"form": form})


@login_required
def my_bookings(request):
    user = request.user
    qs = Booking.objects.select_related("court", "invoice").filter(
        Q(customer=user) | Q(customer_email__iexact=user.email, customer__isnull=True)
    )
    now = timezone.now()
    upcoming = qs.filter(end_at__gte=now).exclude(status__in=Booking.INACTIVE_STATUSES).order_by("start_at")
    past = qs.exclude(pk__in=upcoming.values("pk")).order_by("-start_at")[:30]
    return render(request, "accounts/my_bookings.html", {"upcoming": upcoming, "past": past})


@login_required
def profile(request):
    form = ProfileForm(request.POST or None, instance=request.user)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Profile updated.")
        return redirect("accounts:profile")
    return render(request, "accounts/profile.html", {"form": form})


def magic_login(request, token):
    """Lands a user coming from the platform (signup or platform admin) logged in on this club's site."""
    from tenants.services import check_magic_token

    user = check_magic_token(token, request.club)
    if not user:
        messages.error(request, "That login link has expired. Please log in.")
        return redirect("accounts:login")
    login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    nxt = request.GET.get("next", "/dashboard/")
    if not url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()}):
        nxt = "/dashboard/"
    return redirect(nxt)
