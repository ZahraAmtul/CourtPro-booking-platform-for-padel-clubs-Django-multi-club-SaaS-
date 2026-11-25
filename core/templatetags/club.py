from decimal import Decimal, InvalidOperation

from django import template

from core.models import SiteSettings

register = template.Library()


@register.filter
def money(value, symbol=None):
    """Format a number as currency: 4500 -> 'Rs 4,500'."""
    if value in (None, ""):
        value = 0
    try:
        value = Decimal(value)
    except (InvalidOperation, TypeError):
        return value
    if symbol is None:
        try:
            symbol = SiteSettings.load().currency_symbol
        except LookupError:  # platform pages: no club
            symbol = "Rs"
    text = f"{value:,.0f}" if value == value.to_integral_value() else f"{value:,.2f}"
    return f"{symbol} {text}"


@register.filter
def duration(minutes):
    minutes = int(minutes or 0)
    hours, mins = divmod(minutes, 60)
    if hours and mins:
        return f"{hours}h {mins}m"
    return f"{hours}h" if hours else f"{mins}m"


@register.simple_tag(takes_context=True)
def query_update(context, **kwargs):
    """Rebuild the current querystring with some keys replaced."""
    params = context["request"].GET.copy()
    for key, value in kwargs.items():
        if value in (None, ""):
            params.pop(key, None)
        else:
            params[key] = value
    return "?" + params.urlencode()


@register.filter
def wa_phone(phone):
    """Phone number in WhatsApp click-to-chat format (e.g. 03001234567 -> 923001234567)."""
    from bookings.notifications import normalize_phone

    return normalize_phone(phone)


@register.filter(name="abs")
def absolute(value):
    try:
        return abs(value)
    except TypeError:
        return value
