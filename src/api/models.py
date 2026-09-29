import hashlib
import secrets

from django.conf import settings
from django.db import models
from django.utils import timezone

TOKEN_PREFIX = "ss_"


def hash_token(raw):
    return hashlib.sha256(raw.encode()).hexdigest()


class ApiToken(models.Model):
    """A personal access token: acts as its owner, for scripts and the API.

    Only the hash is stored. The token itself is shown once, when it's made.
    A token stops working when it's revoked, when its owner is deactivated, or
    when its owner changes their password (the same rule as browser sessions).
    """

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="api_tokens")
    name = models.CharField(max_length=80)
    prefix = models.CharField(max_length=12)
    token_hash = models.CharField(max_length=64, unique=True)
    created_at = models.DateTimeField(default=timezone.now)
    last_used_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.name} ({self.prefix}…)"

    @classmethod
    def issue(cls, user, name):
        raw = TOKEN_PREFIX + secrets.token_urlsafe(30)
        token = cls.objects.create(user=user, name=(name or "API token").strip()[:80] or "API token",
                                   prefix=raw[:10], token_hash=hash_token(raw))
        return token, raw

    @property
    def is_active(self):
        return self.revoked_at is None and self.user.is_active
