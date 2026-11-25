from django.shortcuts import render

from bookings.models import Extra
from courts.models import Court


def home(request):
    courts = Court.objects.filter(is_active=True)
    extras = Extra.objects.filter(is_active=True)
    from_price = min((c.price_off_peak for c in courts), default=None)
    return render(request, "core/home.html", {"courts": courts, "extras": extras, "from_price": from_price})
