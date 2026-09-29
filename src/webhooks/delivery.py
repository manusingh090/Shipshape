"""Queueing, signing and sending webhook deliveries.

Queueing: events.models.log_activity calls enqueue() once the write that
logged it has committed (transaction.on_commit), so a rolled-back action is
never announced. Refusals are logged outside any transaction and go at once.

Signing, so a receiver can tell a real delivery from a forged one and an old
one from a replay:

    Shipshape-Signature: t=<unix seconds>,v1=<hex HMAC-SHA256>
    signed message:      "<t>.<exact request body>"
    key:                 the webhook's secret (whsec_...)

Receivers should compare in constant time and refuse a t more than five
minutes old. verify() below is exactly what a receiver does.

Sending: one POST, 5 second timeout, redirects not followed. A 2xx is a
success. Anything else is retried after 1 minute, 5 minutes, 30 minutes, 2
hours and 12 hours, then given up. Five given-up deliveries in a row switch
the webhook off, with the reason on the webhook and in the audit log.
"""

import hashlib
import hmac
import ipaddress
import json
import socket
import time
import urllib.error
import urllib.request
from datetime import timedelta
from urllib.parse import urlsplit

from django.conf import settings
from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone

from .models import Delivery, Webhook

BACKOFF = [60, 300, 1800, 7200, 43200]  # seconds before attempts 2 to 6
MAX_ATTEMPTS = len(BACKOFF) + 1
DISABLE_AFTER = 5
TIMEOUT = 5
TOLERANCE = 300


# ----------------------------------------------------------------- payloads

def payload_for(activity):
    from events.timeutil import iso_utc

    event, project, team, actor = activity.event, activity.project, activity.team, activity.actor
    return {
        "type": activity.verb,
        "created_at": iso_utc(activity.created_at),
        "event": {"slug": event.slug, "name": event.name} if event else None,
        "actor": {"id": actor.pk, "name": actor.display_name} if actor else None,
        "detail": activity.detail,
        "refused": activity.is_refusal,
        "team": {"id": team.pk, "name": team.name} if team else None,
        "project": {"id": project.pk, "title": project.title} if project else None,
        "activity_id": activity.pk,
    }


def _hears(webhook, activity):
    if "all" in webhook.category_list:
        return True
    from events.audit import CATEGORIES
    from events.models import Activity

    rules = [rule for key, _, rule in CATEGORIES if key in webhook.category_list and rule is not None]
    if not rules:
        return False
    query = Q()
    for rule in rules:
        query |= rule
    return Activity.objects.filter(pk=activity.pk).filter(query).exists()


def enqueue(activity_id):
    from events.models import Activity

    activity = Activity.objects.select_related("event", "actor", "team", "project").filter(pk=activity_id).first()
    if activity is None:
        return []
    hooks = Webhook.objects.filter(is_active=True)
    hooks = hooks.filter(event_id=activity.event_id) if activity.event_id else hooks.filter(event__isnull=True)
    made = []
    for hook in hooks:
        if _hears(hook, activity):
            made.append(Delivery.objects.create(webhook=hook, activity=activity, kind=activity.verb,
                                                payload=json.dumps(payload_for(activity))))
    return made


def queue_after_commit(activity):
    transaction.on_commit(lambda: enqueue(activity.pk))


def ping(webhook, actor=None):
    """A test delivery, sent straight away so the page can show the result."""
    delivery = Delivery.objects.create(webhook=webhook, kind="ping", payload=json.dumps({
        "type": "ping", "created_at": timezone.now().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "event": {"slug": webhook.event.slug, "name": webhook.event.name} if webhook.event_id else None,
        "actor": {"id": actor.pk, "name": actor.display_name} if actor else None,
        "detail": "A test delivery from Shipshape. If you can read this, the webhook works.",
    }))
    attempt(delivery)
    return delivery


# ------------------------------------------------------------------ signing

def body_for(delivery):
    """The exact bytes sent: the stored payload plus the delivery's own id."""
    return json.dumps({"id": str(delivery.uid), **json.loads(delivery.payload)}, separators=(",", ":")).encode()


def signature(secret, timestamp, body):
    mac = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={mac}"


def verify(secret, header, body, now=None, tolerance=TOLERANCE):
    """What a receiver does: True if the header signs body with secret and
    isn't older than tolerance seconds."""
    try:
        parts = dict(item.split("=", 1) for item in (header or "").split(","))
        timestamp = int(parts["t"])
    except (ValueError, KeyError):
        return False
    now = int(now if now is not None else time.time())
    if abs(now - timestamp) > tolerance:
        return False
    expected = signature(secret, timestamp, body).split("v1=", 1)[1]
    return hmac.compare_digest(expected, parts.get("v1", ""))


# ------------------------------------------------------------------ sending

class Blocked(Exception):
    pass


def check_destination(url):
    """Refuse destinations that would turn webhooks into a way to poke at the
    server itself: loopback, link-local (cloud metadata lives there),
    multicast, unspecified. Private LAN addresses are allowed, because an
    offline event's receiver is usually on the LAN. WEBHOOK_ALLOW_LOCAL (on in
    demo mode) allows loopback too, for the built-in test receiver."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise Blocked("Only http and https URLs can receive webhooks.")
    try:
        infos = socket.getaddrinfo(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80))
    except socket.gaierror:
        raise Blocked(f"Can't find the host {parts.hostname}.")
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if address.is_loopback and settings.WEBHOOK_ALLOW_LOCAL:
            continue
        if address.is_loopback or address.is_link_local or address.is_multicast or address.is_unspecified:
            raise Blocked(f"{parts.hostname} is {address}, an address webhooks may not be sent to.")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _post(url, body, headers):
    """Returns (status, first 500 characters of the answer)."""
    opener = urllib.request.build_opener(_NoRedirect)
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with opener.open(request, timeout=TIMEOUT) as response:
            return response.status, response.read(500).decode("utf-8", "replace")
    except urllib.error.HTTPError as err:
        return err.code, err.read(500).decode("utf-8", "replace")


def attempt(delivery):
    """One try. Updates the delivery (and the webhook's streak) and returns it."""
    webhook = delivery.webhook
    now = timezone.now()
    body = body_for(delivery)
    timestamp = int(time.time())
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Shipshape-Webhooks/1",
        "Shipshape-Event": delivery.kind,
        "Shipshape-Delivery": str(delivery.uid),
        "Shipshape-Signature": signature(webhook.secret, timestamp, body),
    }
    delivery.attempts += 1
    started = time.monotonic()
    final = False
    try:
        check_destination(webhook.url)
        status, excerpt = _post(webhook.url, body, headers)
        delivery.last_status, delivery.response_excerpt, delivery.last_error = status, excerpt[:500], ""
        ok = 200 <= status < 300
        if not ok:
            delivery.last_error = f"answered {status}"
    except Blocked as err:
        ok, final = False, True
        delivery.last_status, delivery.last_error = None, str(err)[:300]
    except (urllib.error.URLError, OSError, ValueError) as err:
        ok = False
        delivery.last_status, delivery.last_error = None, str(getattr(err, "reason", err))[:300]
    delivery.last_duration_ms = int((time.monotonic() - started) * 1000)
    delivery.locked_until = None

    if ok:
        delivery.status, delivery.delivered_at = Delivery.Status.SUCCEEDED, now
        Webhook.objects.filter(pk=webhook.pk).update(failure_streak=0)
    elif final or delivery.attempts >= MAX_ATTEMPTS or delivery.kind == "ping":
        delivery.status = Delivery.Status.FAILED
        if delivery.kind != "ping":
            _count_failure(webhook)
    else:
        delivery.next_attempt_at = now + timedelta(seconds=BACKOFF[delivery.attempts - 1])
    delivery.save()
    return delivery


def _count_failure(webhook):
    Webhook.objects.filter(pk=webhook.pk).update(failure_streak=F("failure_streak") + 1)
    webhook.refresh_from_db()
    if webhook.failure_streak >= DISABLE_AFTER and webhook.is_active:
        webhook.is_active = False
        webhook.disabled_reason = f"Switched off after {DISABLE_AFTER} deliveries in a row gave up."
        webhook.save(update_fields=["is_active", "disabled_reason"])
        from events.models import log_activity

        log_activity(webhook.event, None, "webhook.disabled",
                     f"switched off the webhook to {webhook.url}: {DISABLE_AFTER} deliveries in a row failed")


def deliver_due(limit=25):
    """Send what's due. Several processes can call this at once: each claims a
    delivery with a guarded update before sending it."""
    now = timezone.now()
    due = list(Delivery.objects.filter(status=Delivery.Status.PENDING, next_attempt_at__lte=now,
                                       webhook__is_active=True)
               .filter(Q(locked_until__isnull=True) | Q(locked_until__lt=now))
               .order_by("next_attempt_at", "id").values_list("pk", flat=True)[:limit])
    sent = 0
    for pk in due:
        claimed = Delivery.objects.filter(pk=pk, status=Delivery.Status.PENDING).filter(
            Q(locked_until__isnull=True) | Q(locked_until__lt=now)).update(locked_until=now + timedelta(minutes=2))
        if claimed:
            attempt(Delivery.objects.select_related("webhook__event").get(pk=pk))
            sent += 1
    return sent
