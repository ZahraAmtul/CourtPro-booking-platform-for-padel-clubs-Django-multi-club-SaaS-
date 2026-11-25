"""Subscription plans you sell to clubs. Edit prices/limits here."""
from decimal import Decimal

PLANS = {
    "starter": {
        "label": "Starter",
        "price": Decimal("7000"),  # PKR / month
        "max_courts": 2,
        "whatsapp": False,
        "blurb": "Up to 2 courts · online booking · invoices · deposits",
        "tagline": "For a club with one or two courts",
        "features": [
            "Your own booking website",
            "Up to 2 courts",
            "Deposits by JazzCash",
            "Invoices and payment tracking",
            "Club credit and packages",
            "End-of-day cash report",
            "Staff logins",
        ],
    },
    "pro": {
        "label": "Pro",
        "price": Decimal("15000"),
        "max_courts": None,  # unlimited
        "whatsapp": True,
        "blurb": "Unlimited courts · everything in Starter · automatic WhatsApp reminders",
        "tagline": "For busy clubs that want every slot filled",
        "features": [
            "Everything in Starter",
            "Unlimited courts",
            "Automatic WhatsApp confirmations and reminders",
            "Weekly recurring bookings",
            "Your own domain name",
        ],
    },
}
DEFAULT_PLAN = "pro"  # new clubs trial the full product
TRIAL_DAYS = 14
GRACE_DAYS = 7  # after trial/paid period ends, keep working this long with a warning
