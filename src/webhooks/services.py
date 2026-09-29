"""Managing webhooks. The console page and the REST API both call these."""

from urllib.parse import urlsplit

from django import forms

from events.access import Viewer
from events.audit import CATEGORIES
from events.models import log_activity

from . import delivery as sender
from .models import Delivery, Webhook, new_secret


class WebhookError(Exception):
    status = 400
    code = "webhook_error"

    def __init__(self, message):
        super().__init__(message)
        self.message = message


def category_choices(event):
    """What a webhook can listen for: the audit log's own categories."""
    keys = (["refused", "integrity", "judging", "voting", "submissions", "records", "settings"] if event
            else ["refused", "accounts"])
    labels = {key: label for key, label, _ in CATEGORIES}
    return [("all", "Everything")] + [(k, labels[k]) for k in keys]


class WebhookForm(forms.ModelForm):
    categories = forms.MultipleChoiceField(widget=forms.CheckboxSelectMultiple, label="Send",
                                           help_text="The same groups the activity log filters by.")

    class Meta:
        model = Webhook
        fields = ["url", "description", "categories"]
        help_texts = {"url": "Where to POST. http or https; a LAN address is fine on an offline network.",
                      "description": "Optional. What it's for, e.g. “Discord #judges”."}

    def __init__(self, *args, event=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["categories"].choices = category_choices(event)
        if self.instance.pk:
            self.initial["categories"] = self.instance.category_list
        else:
            self.initial.setdefault("categories", ["all"])

    def clean_url(self):
        url = self.cleaned_data["url"].strip()
        if urlsplit(url).scheme not in ("http", "https"):
            raise forms.ValidationError("Webhooks go to http or https URLs.")
        return url

    def clean_categories(self):
        chosen = self.cleaned_data["categories"]
        return ["all"] if "all" in chosen else chosen

    def save(self, commit=True):
        hook = super().save(commit=False)
        hook.categories = ",".join(self.cleaned_data["categories"])
        if commit:
            hook.save()
        return hook


def can_manage(user, hook_or_event):
    event = hook_or_event.event if isinstance(hook_or_event, Webhook) else hook_or_event
    if event is None:
        return user.is_authenticated and user.is_admin
    return Viewer(user, event).is_organizer


def _where(hook):
    return hook.event  # None for platform webhooks, which log to the platform audit log


def create(actor, event, form):
    hook = form.save(commit=False)
    hook.event, hook.created_by = event, actor
    hook.categories = ",".join(form.cleaned_data["categories"])
    hook.save()
    log_activity(event, actor, "webhook.created", f"added a webhook to {hook.url} ({hook.categories})")
    return hook


def update(actor, hook, form=None, is_active=None):
    if form is not None:
        hook = form.save()
    if is_active is not None and is_active != hook.is_active:
        hook.is_active = is_active
        if is_active:
            hook.failure_streak, hook.disabled_reason = 0, ""
        hook.save(update_fields=["is_active", "failure_streak", "disabled_reason"])
    log_activity(_where(hook), actor, "webhook.updated",
                 f"changed the webhook to {hook.url} ({'on' if hook.is_active else 'off'}, {hook.categories})")
    return hook


def delete(actor, hook):
    url, event = hook.url, _where(hook)
    hook.delete()
    log_activity(event, actor, "webhook.deleted", f"deleted the webhook to {url}")
    return url


def rotate_secret(actor, hook):
    hook.secret = new_secret()
    hook.save(update_fields=["secret"])
    log_activity(_where(hook), actor, "webhook.secret", f"made a new signing secret for the webhook to {hook.url}")
    return hook


def test(actor, hook):
    return sender.ping(hook, actor)


def redeliver(actor, original):
    """Send the same payload again, as a new delivery with a fresh id."""
    copy = Delivery.objects.create(webhook=original.webhook, activity=original.activity, kind=original.kind,
                                   payload=original.payload)
    sender.attempt(copy)
    return copy


def self_test_url(hook):
    from django.conf import settings
    from django.urls import reverse

    return settings.SELF_URL + reverse("webhook_receiver", args=[hook.pk])
