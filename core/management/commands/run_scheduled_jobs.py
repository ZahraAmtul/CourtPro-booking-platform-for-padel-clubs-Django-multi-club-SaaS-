"""Background jobs for every club: release unpaid holds, send reminders, retry failed confirmations.

Run every few minutes:
    python manage.py run_scheduled_jobs              # once (cron: */5 * * * *)
    python manage.py run_scheduled_jobs --loop 120   # forever, every 2 minutes (Docker service)
"""
import time

from django.core.management.base import BaseCommand
from django.db import close_old_connections

from bookings.notifications import retry_failed, send_due_reminders
from bookings.services import expire_unpaid_bookings
from tenants.context import use_club
from tenants.models import Club


class Command(BaseCommand):
    help = "Expire unpaid booking holds and send WhatsApp/email reminders for all clubs"

    def add_arguments(self, parser):
        parser.add_argument("--loop", type=int, default=0, help="Repeat every N seconds")

    def handle(self, *args, **opts):
        while True:
            close_old_connections()
            for club in Club.objects.filter(is_suspended=False):
                try:
                    with use_club(club):
                        expired = expire_unpaid_bookings()
                        reminders = send_due_reminders() if club.is_operational else 0
                        retried = retry_failed() if club.is_operational else 0
                    if expired or reminders or retried:
                        self.stdout.write(f"[{club.slug}] expired={expired} reminders={reminders} retried={retried}")
                except Exception as exc:  # one club's problem must not stop the others
                    self.stderr.write(f"[{club.slug}] scheduled jobs failed: {exc!r}")
                    if not opts["loop"]:
                        raise
            if not opts["loop"]:
                break
            time.sleep(opts["loop"])
