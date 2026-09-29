import os
import sys

from django.apps import AppConfig


class WebhooksAppConfig(AppConfig):
    name = "webhooks"
    verbose_name = "Webhooks"

    def ready(self):
        # The container's entrypoint sets WEBHOOK_WORKER=1 for gunicorn, so
        # every web process delivers in the background. Tests, the seed and
        # other management commands never start it.
        if os.environ.get("WEBHOOK_WORKER") == "1" and "test" not in sys.argv[1:2]:
            from .worker import start

            start()
