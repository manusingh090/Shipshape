"""The Webhooks tab in the organizer console, the admin's platform webhooks,
and a built-in receiver that checks signatures, for trying webhooks offline."""

import json

from django.contrib import messages
from django.http import HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.csrf import csrf_exempt

from accounts.decorators import admin_required, login_required
from events.access import get_event_for, require_organizer

from . import delivery as sender
from . import services
from .models import Delivery, Webhook


def _page(request, event, template_context):
    hooks = Webhook.objects.filter(event=event) if event else Webhook.objects.filter(event__isnull=True)
    form = services.WebhookForm(event=event)
    editing = None

    if request.method == "POST":
        op = request.POST.get("op")
        hook = get_object_or_404(hooks, pk=request.POST.get("id")) if request.POST.get("id") else None
        if op == "create":
            form = services.WebhookForm(request.POST, event=event)
            if form.is_valid():
                hook = services.create(request.user, event, form)
                messages.success(request, "Webhook added. Send a test to check it's listening.")
                return redirect(f"{request.path}#hook-{hook.pk}")
        elif op == "save" and hook:
            editing = hook
            form = services.WebhookForm(request.POST, instance=hook, event=event)
            if form.is_valid():
                services.update(request.user, hook, form)
                messages.success(request, "Webhook saved.")
                return redirect(f"{request.path}#hook-{hook.pk}")
        elif op in ("on", "off") and hook:
            services.update(request.user, hook, is_active=op == "on")
            messages.success(request, f"Webhook switched {op}.")
            return redirect(f"{request.path}#hook-{hook.pk}")
        elif op == "delete" and hook:
            services.delete(request.user, hook)
            messages.success(request, "Webhook deleted.")
            return redirect(request.path)
        elif op == "rotate" and hook:
            services.rotate_secret(request.user, hook)
            messages.success(request, "New signing secret made. Update your receiver: the old one stops matching now.")
            return redirect(f"{request.path}#hook-{hook.pk}")
        elif op == "test" and hook:
            d = services.test(request.user, hook)
            if d.status == Delivery.Status.SUCCEEDED:
                messages.success(request, f"Test delivered: {d.last_status} in {d.last_duration_ms} ms.")
            else:
                messages.error(request, f"Test didn't arrive: {d.last_error or d.last_status}.")
            return redirect(f"{request.path}#hook-{hook.pk}")
        elif op == "redeliver" and hook:
            original = get_object_or_404(Delivery, pk=request.POST.get("delivery"), webhook=hook)
            d = services.redeliver(request.user, original)
            messages.info(request, f"Sent again: {d.get_status_display().lower()}"
                                   f"{f' ({d.last_status})' if d.last_status else ''}.")
            return redirect(f"{request.path}#hook-{hook.pk}")
        elif op == "use_receiver" and hook:
            hook.url = services.self_test_url(hook)
            hook.save(update_fields=["url"])
            messages.success(request, "Pointed at the built-in test receiver. Send a test.")
            return redirect(f"{request.path}#hook-{hook.pk}")
        else:
            return HttpResponseBadRequest("Unknown operation.")

    rows = [{"hook": h, "deliveries": list(h.deliveries.all()[:15]),
             "edit_form": services.WebhookForm(instance=h, event=event), "self_test": services.self_test_url(h)}
            for h in hooks]
    return render(request, "webhooks/manage.html", {
        **template_context, "rows": rows, "form": form if not editing else services.WebhookForm(event=event),
        "backoff": [m // 60 for m in sender.BACKOFF], "disable_after": sender.DISABLE_AFTER,
    })


@login_required
def manage(request, slug):
    event, viewer = get_event_for(request, slug)
    require_organizer(viewer)
    return _page(request, event, {"event": event, "viewer": viewer, "section": "webhooks", "platform": False})


@admin_required
def admin_manage(request):
    return _page(request, None, {"platform": True})


@csrf_exempt
def receiver(request, pk):
    """A receiver built into the portal, for trying webhooks with no network:
    point a webhook here (the page has a button) and every delivery is
    checked exactly as your own receiver should check it."""
    if request.method != "POST":
        return JsonResponse({"error": "method_not_allowed"}, status=405)
    hook = Webhook.objects.filter(pk=pk).first()
    if hook is None:
        return JsonResponse({"verified": False, "error": "no such webhook"}, status=404)
    ok = sender.verify(hook.secret, request.headers.get("Shipshape-Signature", ""), request.body)
    if not ok:
        return JsonResponse({"verified": False, "error": "signature doesn't match, or it's too old"}, status=400)
    try:
        kind = json.loads(request.body).get("type")
    except ValueError:
        kind = None
    return JsonResponse({"verified": True, "received": kind})
