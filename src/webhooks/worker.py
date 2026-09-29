"""The background sender: one daemon thread per web process.

Every couple of seconds it sends whatever is due. Deliveries are claimed with
a guarded update (delivery.deliver_due), so three gunicorn workers never send
the same one twice. `python src/manage.py deliver_webhooks` does the same job
from a terminal or cron, for a setup without the container.
"""

import logging
import threading
import time

from django.db import close_old_connections

log = logging.getLogger(__name__)
_started = False
INTERVAL = 2.0


def _loop():
    from .delivery import deliver_due

    while True:
        try:
            close_old_connections()
            deliver_due()
        except Exception:  # never let one bad delivery stop the sender
            log.exception("webhook delivery loop")
        finally:
            close_old_connections()
        time.sleep(INTERVAL)


def start():
    global _started
    if _started:
        return
    _started = True
    threading.Thread(target=_loop, name="webhook-sender", daemon=True).start()
