import time

from django.core.management.base import BaseCommand

from webhooks.delivery import deliver_due


class Command(BaseCommand):
    help = "Send webhook deliveries that are due. --loop keeps going every couple of seconds."

    def add_arguments(self, parser):
        parser.add_argument("--loop", action="store_true")

    def handle(self, *args, loop=False, **options):
        while True:
            sent = deliver_due()
            if sent or not loop:
                self.stdout.write(f"sent {sent} deliver{'y' if sent == 1 else 'ies'}")
            if not loop:
                return
            time.sleep(2)
