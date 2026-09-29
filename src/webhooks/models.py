"""Webhooks: an organizer's URL that hears about what happens in their event.

Every entry the portal writes to an event's audit log (events.Activity) can be
sent, so anything the portal logs is covered, in the same words. Platform
webhooks (no event, admins only) hear the platform log: new accounts, sign-in
lockouts, admin changes.
"""

import secrets
import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone


def new_secret():
    return "whsec_" + secrets.token_urlsafe(32)


class Webhook(models.Model):
    event = models.ForeignKey("events.Event", null=True, blank=True, on_delete=models.CASCADE, related_name="webhooks")
    url = models.URLField("URL", max_length=500)
    description = models.CharField(max_length=140, blank=True)
    # Audit categories (events/audit.py) this webhook hears; "all" for everything.
    categories = models.CharField(max_length=200, default="all")
    secret = models.CharField(max_length=80, default=new_secret)
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(default=timezone.now)
    failure_streak = models.PositiveIntegerField(default=0)
    disabled_reason = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["created_at", "id"]

    def __str__(self):
        return self.url

    @property
    def category_list(self):
        return [c for c in self.categories.split(",") if c]


class Delivery(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Waiting"
        SUCCEEDED = "succeeded", "Delivered"
        FAILED = "failed", "Gave up"

    webhook = models.ForeignKey(Webhook, on_delete=models.CASCADE, related_name="deliveries")
    activity = models.ForeignKey("events.Activity", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    uid = models.UUIDField(default=uuid.uuid4, unique=True)
    kind = models.CharField(max_length=40)
    payload = models.TextField()
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING, db_index=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    next_attempt_at = models.DateTimeField(default=timezone.now, db_index=True)
    locked_until = models.DateTimeField(null=True, blank=True)
    last_status = models.PositiveSmallIntegerField(null=True, blank=True)
    last_error = models.CharField(max_length=300, blank=True)
    last_duration_ms = models.PositiveIntegerField(null=True, blank=True)
    response_excerpt = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    delivered_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return f"{self.kind} to {self.webhook_id}"
