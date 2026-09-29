"""Certificates and records: signed, verifiable statements about an event.

A record's `payload` is the exact JSON that was signed, frozen when it was
issued. The signature is Ed25519 (records/ed25519.py) with the portal's
signing key, so anyone holding the public key can check a record without the
portal, long after the laptop that ran the event is gone.

`private` holds what the public record only commits to: for a judge, the
scores behind the SHA-256 in the payload. Only the judge and the event's
organizers ever see it.
"""

import secrets

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone

ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"  # no 0/O or 1/I, so a code can be read aloud


def new_code():
    raw = "".join(secrets.choice(ALPHABET) for _ in range(16))  # 80 bits
    return "-".join(raw[i:i + 4] for i in range(0, 16, 4))


class Record(models.Model):
    class Kind(models.TextChoices):
        PARTICIPANT = "participant", "Certificate of participation"
        AWARD = "award", "Winner's certificate"
        JUDGE = "judge", "Judge's record"
        ORGANIZER = "organizer", "Organizer's certificate"

    event = models.ForeignKey("events.Event", on_delete=models.CASCADE, related_name="records")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="records")
    kind = models.CharField(max_length=12, choices=Kind.choices)
    # What makes this record unique for its person: "award:12", "team:4", "judge", "organizer".
    subject = models.CharField(max_length=40)
    code = models.CharField(max_length=19, unique=True, default=new_code)
    payload = models.TextField()
    signature = models.CharField(max_length=128)
    key_id = models.CharField(max_length=16)
    private = models.TextField(blank=True)
    issued_at = models.DateTimeField(default=timezone.now)
    issued_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    revoked_at = models.DateTimeField(null=True, blank=True)
    revoked_reason = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["-issued_at", "id"]
        constraints = [
            models.UniqueConstraint(fields=["event", "user", "kind", "subject"], condition=Q(revoked_at__isnull=True),
                                    name="one_live_record_per_subject"),
        ]

    def __str__(self):
        return f"{self.get_kind_display()} {self.code}"

    @property
    def is_revoked(self):
        return self.revoked_at is not None
