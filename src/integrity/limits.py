"""Every rate limit in the portal, in one table, and how refusals are logged.

Limits are set to stop scripts and floods, not crowds: a hackathon venue puts
hundreds of honest people behind one network address, so per-network limits
are generous and the suspicious-but-possible patterns (ten ballots from one
network in ten minutes) are flagged for an organizer to judge instead of
refused (see integrity/detect.py).

When a limit refuses someone, one line goes into the audit log for that key
per window, not one per request, so a script hammering an endpoint leaves a
readable trail instead of burying everything else.
"""

import hashlib
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from events.models import log_activity

from .models import Throttle


@dataclass(frozen=True)
class Limit:
    scope: str
    what: str           # "sign-in failures for one address"
    limit: int
    window: timedelta
    per: str            # "address", "network", "person", ...
    why: str

    @property
    def window_label(self):
        minutes = int(self.window.total_seconds() // 60)
        if minutes % 60 == 0:
            hours = minutes // 60
            return "hour" if hours == 1 else f"{hours} hours"
        return "minute" if minutes == 1 else f"{minutes} minutes"


MINUTE, HOUR = timedelta(minutes=1), timedelta(hours=1)

LIMITS = {limit.scope: limit for limit in [
    Limit("login.address", "wrong passwords for one address", 5, timedelta(minutes=15), "address",
          "Password guessing against one account."),
    Limit("login.network", "wrong passwords from one network", 30, timedelta(minutes=15), "network",
          "Password guessing across many accounts."),
    Limit("signup.network", "new accounts from one network", 20, HOUR, "network",
          "Scripted sign-ups, which would be fake voters in a signed-in vote."),
    Limit("submission.save", "project saves by one person", 30, MINUTE, "person",
          "Hammering the submission form or API."),
    Limit("score.save", "score saves by one judge", 30, MINUTE, "judge",
          "A scripted or runaway judging session."),
    Limit("vote.link_address", "voting links sent to one address", 3, timedelta(minutes=15), "address",
          "Mail-bombing someone through the email-gated vote."),
    Limit("vote.link_network", "voting links asked for from one network", 20, HOUR, "network",
          "Collecting addresses to vote with."),
    Limit("vote.new_ballot", "new ballots from one network", 100, HOUR, "network",
          "Ballot stuffing by script. Smaller bursts are flagged for review instead."),
    Limit("vote.save", "ballot saves by one voter", 20, MINUTE, "voter",
          "Hammering the ballot."),
    Limit("api.write", "API writes by one person", 120, MINUTE, "person",
          "A runaway script. The limits above still apply to each action as well."),
    Limit("comment.minute", "comments by one person", 5, MINUTE, "person", "Comment floods."),
    Limit("comment.hour", "comments by one person", 30, HOUR, "person", "Comment spam."),
]}


class Limited(Exception):
    status = 429
    code = "slow_down"

    def __init__(self, message):
        super().__init__(message)
        self.message = message


def network_key(ip):
    """A salted hash of an address, the same one ballots store. Raw addresses
    are never kept."""
    if not ip:
        return ""
    return hashlib.sha256(f"{settings.SECRET_KEY}:{ip}".encode()).hexdigest()


def _since(limit):
    return timezone.now() - limit.window


def used(scope, key):
    """How many counted actions this key has in the current window."""
    limit = LIMITS[scope]
    return Throttle.objects.filter(scope=scope, key=key, refused=False, created_at__gte=_since(limit)).count()


def record(scope, key):
    """Count one action for a Throttle-backed limit. Old rows are swept."""
    if not key:
        return
    Throttle.objects.create(scope=scope, key=str(key))
    Throttle.objects.filter(created_at__lt=timezone.now() - timedelta(days=1)).delete()


def check(scope, key, message, *, count=None, exc=Limited, event=None, actor=None, detail=""):
    """Raise exc(message) if key has used up this limit.

    count: pass how many actions the key already has when a table of its own
    already records them (comments, scores); leave it out to count Throttle
    rows. The refusal is logged once per key per window.
    """
    if not key:
        return
    limit = LIMITS[scope]
    if count is None:
        count = used(scope, key)
    if count < limit.limit:
        return
    note_refusal(scope, key, event=event, actor=actor, detail=detail)
    raise exc(message)


def note_refusal(scope, key, *, event=None, actor=None, detail=""):
    limit = LIMITS[scope]
    already = Throttle.objects.filter(scope=scope, key=str(key), refused=True, created_at__gte=_since(limit)).exists()
    if already:
        return False
    Throttle.objects.create(scope=scope, key=str(key), refused=True)
    text = f"hit a rate limit: {limit.limit} {limit.what} per {limit.window_label}"
    if detail:
        text += f" ({detail})"
    log_activity(event, actor, "rate.limited", text + ". Further refusals in this window aren't logged.")
    return True


def recent_refusals(event=None, since=None):
    """Audit lines for rate-limit refusals, for the integrity page."""
    from events.models import Activity

    qs = Activity.objects.filter(verb="rate.limited")
    qs = qs.filter(event=event) if event is not None else qs.filter(event__isnull=True)
    if since:
        qs = qs.filter(created_at__gte=since)
    return qs
