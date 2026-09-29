"""Slow down password guessing.

Five wrong passwords for one email inside fifteen minutes locks that email
for the rest of the window. A single address hammering many accounts gets
cut off at thirty failures. Counts live in the database, so every gunicorn
worker sees the same numbers.
"""

from datetime import timedelta

from django.utils import timezone

from .models import LoginAttempt

WINDOW = timedelta(minutes=15)
MAX_FAILURES_PER_EMAIL = 5
MAX_FAILURES_PER_IP = 30


def client_ip(request):
    # REMOTE_ADDR only. X-Forwarded-For is trivially forged and there is no
    # proxy in front of this app by default.
    return request.META.get("REMOTE_ADDR") or None


def lockout_remaining(email, ip, now=None):
    """Return how long this login is blocked for, or None if it may proceed."""
    now = now or timezone.now()
    since = now - WINDOW

    last_success = (
        LoginAttempt.objects.filter(email=email, succeeded=True, created_at__gte=since)
        .order_by("-created_at")
        .values_list("created_at", flat=True)
        .first()
    )
    email_failures = LoginAttempt.objects.filter(
        email=email, succeeded=False, created_at__gte=last_success or since
    ).order_by("-created_at")
    recent = list(email_failures.values_list("created_at", flat=True)[:MAX_FAILURES_PER_EMAIL])
    if len(recent) >= MAX_FAILURES_PER_EMAIL:
        return max(recent[-1] + WINDOW - now, timedelta(seconds=1))

    if ip:
        ip_failures = LoginAttempt.objects.filter(ip=ip, succeeded=False, created_at__gte=since)
        oldest = list(ip_failures.order_by("-created_at").values_list("created_at", flat=True)[:MAX_FAILURES_PER_IP])
        if len(oldest) >= MAX_FAILURES_PER_IP:
            return max(oldest[-1] + WINDOW - now, timedelta(seconds=1))
    return None


def record(email, ip, succeeded):
    LoginAttempt.objects.create(email=email, ip=ip, succeeded=succeeded)
    # Housekeeping: nothing older than a day is ever consulted.
    LoginAttempt.objects.filter(created_at__lt=timezone.now() - timedelta(days=1)).delete()
