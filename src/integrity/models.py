from django.db import models
from django.utils import timezone


class Throttle(models.Model):
    """One counted action for a rate limit that has no table of its own to
    count (sign-ups per network, ballot saves, submission saves), plus a
    marker the first time a key is refused in a window, so the audit log gets
    one line per burst rather than one per request."""

    scope = models.CharField(max_length=40)
    key = models.CharField(max_length=128)
    refused = models.BooleanField(default=False)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        indexes = [models.Index(fields=["scope", "key", "created_at"], name="throttle_lookup")]

    def __str__(self):
        return f"{self.scope} {self.key}"
