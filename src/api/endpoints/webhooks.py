"""Webhooks over the API: an event's (organizers) and the platform's (admins)."""

import json

from django.shortcuts import get_object_or_404

from webhooks import services
from webhooks.models import Delivery, Webhook

from .. import schema as sc
from .. import serializers as s
from ..core import ApiError, endpoint, json_body, paginate, valid
from .events import event_for


# What each endpoint takes and returns (api/schema.py), for the OpenAPI document.
CATEGORY = sc.enum("all", "refused", "integrity", "judging", "voting", "submissions", "settings", "records", "accounts")
WEBHOOK = sc.component("Webhook", sc.obj(
    id=sc.integer(), event=sc.nullable(sc.string("The event's slug; null for a platform webhook.")), url=sc.url(),
    description=sc.string(), categories=sc.array(CATEGORY, "What it hears: all, or the audit log's categories."),
    is_active=sc.boolean(), failure_streak=sc.integer("Given-up deliveries in a row; five switch it off."),
    disabled_reason=sc.nullable(sc.string()), created_at=sc.when(),
    secret=sc.optional(sc.string("The signing secret (whsec_...). Only to the people who manage it."))))
PAYLOAD = sc.component("WebhookPayload", sc.obj(
    "What each delivery POSTs, signed with Shipshape-Signature: t=<unix seconds>,v1=<hex HMAC-SHA256 of "
    "\"<t>.<body>\" keyed with the secret>. Refuse a t more than five minutes old.",
    id=sc.optional(sc.string("The delivery's id (the Shipshape-Delivery header), to spot repeats.")),
    type=sc.string("What happened, e.g. project.submitted, or ping for a test."), created_at=sc.when(),
    event=sc.nullable(sc.obj(slug=sc.string(), name=sc.string())), actor=sc.nullable(sc.ref("Person")),
    detail=sc.string(), refused=sc.optional(sc.boolean()),
    team=sc.optional(sc.nullable(sc.obj(id=sc.integer(), name=sc.string()))),
    project=sc.optional(sc.nullable(sc.obj(id=sc.integer(), title=sc.string()))),
    activity_id=sc.optional(sc.integer("The audit log entry."))))
DELIVERY = sc.component("Delivery", sc.obj(
    id=sc.integer(), uid=sc.string(), type=sc.string(), status=sc.enum("pending", "succeeded", "failed"),
    attempts=sc.integer(), next_attempt_at=sc.nullable(sc.when()),
    last_status=sc.nullable(sc.integer("The receiver's HTTP status.")), last_error=sc.nullable(sc.string()),
    duration_ms=sc.nullable(sc.integer()), response=sc.nullable(sc.string("The start of the receiver's answer.")),
    created_at=sc.when(), delivered_at=sc.nullable(sc.when()), payload=PAYLOAD))
HOOK_BODY = sc.obj(url=sc.url("Where to POST. Addresses on the server itself are refused."),
                   description=sc.optional(sc.string()),
                   categories=sc.optional(sc.any_of(sc.array(CATEGORY), sc.string(), description="A list, or comma "
                                                    "separated. Defaults to all.")))
HOOK_UPDATE = sc.obj(url=sc.optional(sc.url()), description=sc.optional(sc.string()),
                     categories=sc.optional(sc.any_of(sc.array(CATEGORY), sc.string())),
                     is_active=sc.optional(sc.boolean("Switching on clears the failure streak.")))


def hook_json(h, secret=False):
    data = {"id": h.id, "event": h.event.slug if h.event_id else None, "url": h.url, "description": h.description,
            "categories": h.category_list, "is_active": h.is_active, "failure_streak": h.failure_streak,
            "disabled_reason": h.disabled_reason or None, "created_at": s.when(h.created_at)}
    if secret:
        data["secret"] = h.secret
    return data


def delivery_json(d):
    return {"id": d.pk, "uid": str(d.uid), "type": d.kind, "status": d.status, "attempts": d.attempts,
            "next_attempt_at": s.when(d.next_attempt_at) if d.status == "pending" else None,
            "last_status": d.last_status, "last_error": d.last_error or None, "duration_ms": d.last_duration_ms,
            "response": d.response_excerpt or None, "created_at": s.when(d.created_at),
            "delivered_at": s.when(d.delivered_at), "payload": json.loads(d.payload)}


def _form_data(body, hook=None):
    data = {"url": hook.url if hook else "", "description": hook.description if hook else "",
            "categories": hook.category_list if hook else ["all"]}
    data.update({k: v for k, v in body.items() if k in data})
    if isinstance(data["categories"], str):
        data["categories"] = [c.strip() for c in data["categories"].split(",") if c.strip()]
    return data


def _hook(request, pk):
    hook = get_object_or_404(Webhook.objects.select_related("event"), pk=pk)
    if not services.can_manage(request.user, hook):
        raise ApiError(404, "not_found", "No such webhook, or not yours to manage.")
    return hook


def _create(request, event):
    form = valid(services.WebhookForm(_form_data(json_body(request)), event=event))
    return hook_json(services.create(request.user, event, form), secret=True)


@endpoint("GET", "events/<slug:slug>/webhooks", who="organizer", summary="The event's webhooks.",
          ui="Console, Webhooks",
          returns=sc.results(WEBHOOK))
def event_hooks(request, slug):
    event, _ = event_for(request, slug, organizer=True)
    return {"results": [hook_json(h) for h in Webhook.objects.filter(event=event)]}


@endpoint("POST", "events/<slug:slug>/webhooks", who="organizer",
          summary="Add a webhook. The answer includes its signing secret.", ui="Console, Webhooks, Add webhook",
          body='{"url": "https://...", "description": "", "categories": ["all"] or ["submissions", "refused", ...]}',
          request=HOOK_BODY, returns=WEBHOOK)
def create_event_hook(request, slug):
    event, _ = event_for(request, slug, organizer=True)
    return _create(request, event)


@endpoint("GET", "admin/webhooks", who="admin", summary="Platform webhooks (the platform audit log).",
          ui="Admin, Platform webhooks",
          returns=sc.results(WEBHOOK))
def platform_hooks(request):
    if not request.user.is_admin:
        raise ApiError(403, "forbidden", "Platform admins only.")
    return {"results": [hook_json(h) for h in Webhook.objects.filter(event__isnull=True)]}


@endpoint("POST", "admin/webhooks", who="admin", summary="Add a platform webhook.", ui="Admin, Platform webhooks, Add",
          body='{"url": "https://...", "categories": ["all"] or ["refused", "accounts"]}',
          request=HOOK_BODY, returns=WEBHOOK)
def create_platform_hook(request):
    if not request.user.is_admin:
        raise ApiError(403, "forbidden", "Platform admins only.")
    return _create(request, None)


@endpoint("GET", "webhooks/<int:pk>", who="organizer (or admin, for platform ones)",
          summary="One webhook, with its signing secret.", ui="Console, Webhooks, Signing secret",
          returns=WEBHOOK)
def get_hook(request, pk):
    return hook_json(_hook(request, pk), secret=True)


@endpoint("PATCH", "webhooks/<int:pk>", who="organizer (or admin)",
          summary="Change a webhook, or switch it on or off. Switching on clears a failure streak.",
          ui="Console, Webhooks, Edit / Switch on / Switch off",
          body='{"url": ..., "categories": [...], "is_active": true}',
          request=HOOK_UPDATE, returns=WEBHOOK)
def update_hook(request, pk):
    hook = _hook(request, pk)
    body = json_body(request)
    form = None
    if any(k in body for k in ("url", "description", "categories")):
        form = valid(services.WebhookForm(_form_data(body, hook), instance=hook, event=hook.event))
    active = body.get("is_active")
    if active is not None and not isinstance(active, bool):
        raise ApiError(400, "invalid", '"is_active" is true or false.')
    return hook_json(services.update(request.user, hook, form, is_active=active), secret=True)


@endpoint("DELETE", "webhooks/<int:pk>", who="organizer (or admin)", summary="Delete a webhook and its delivery log.",
          ui="Console, Webhooks, Delete",
          returns=sc.DELETED)
def delete_hook(request, pk):
    return {"deleted": services.delete(request.user, _hook(request, pk))}


@endpoint("POST", "webhooks/<int:pk>/secret", who="organizer (or admin)",
          summary="Make a new signing secret; the old one stops matching at once.",
          ui="Console, Webhooks, Make a new secret",
          returns=WEBHOOK)
def rotate(request, pk):
    return hook_json(services.rotate_secret(request.user, _hook(request, pk)), secret=True)


@endpoint("POST", "webhooks/<int:pk>/test", who="organizer (or admin)",
          summary="Send a test delivery now and say what came back.", ui="Console, Webhooks, Send a test",
          returns=DELIVERY)
def test_hook(request, pk):
    return delivery_json(services.test(request.user, _hook(request, pk)))


@endpoint("GET", "webhooks/<int:pk>/deliveries", who="organizer (or admin)",
          summary="The delivery log, newest first; ?status=pending|succeeded|failed.", ui="Console, Webhooks, Recent deliveries",
          query={"status": sc.enum("pending", "succeeded", "failed")}, returns=sc.page(DELIVERY))
def deliveries(request, pk):
    hook = _hook(request, pk)
    rows = hook.deliveries.all()
    if request.GET.get("status") in Delivery.Status.values:
        rows = rows.filter(status=request.GET["status"])
    return paginate(request, rows, delivery_json)


@endpoint("POST", "webhooks/<int:pk>/deliveries/<int:delivery_id>/redeliver", who="organizer (or admin)",
          summary="Send a delivery's payload again, now, as a new delivery.", ui="Console, Webhooks, Send again",
          returns=DELIVERY)
def redeliver(request, pk, delivery_id):
    hook = _hook(request, pk)
    original = get_object_or_404(Delivery, pk=delivery_id, webhook=hook)
    return delivery_json(services.redeliver(request.user, original))
