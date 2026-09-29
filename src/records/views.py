"""Certificates on the web: each one's page, its PDF and signed JSON, a
judge's private receipt, the verify page, and the organizer's console tab."""

import json

from django.contrib import messages
from django.http import Http404, HttpResponse, HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from accounts.decorators import login_required
from events.access import get_event_for, require_organizer
from events.models import Prize
from judging.results import compute_results
from projects.models import Project

from . import render as drawing
from . import services, signing
from .models import Record


def _record(code):
    return get_object_or_404(Record.objects.select_related("event", "user"), code=(code or "").strip().upper())


def verify_url(request, record):
    return request.build_absolute_uri(reverse("records:verify_code", args=[record.code]))


def detail(request, code):
    """Anyone with the code can see the record: that's how a certificate is
    checked. It only ever shows what the signed record says."""
    record = _record(code)
    payload = json.loads(record.payload)
    tail = payload["statement"]
    if tail.startswith(payload["recipient"] + " "):
        tail = tail[len(payload["recipient"]) + 1:]
    return render(request, "records/detail.html", {
        "record": record, "payload": payload, "statement_tail": tail, "status": services.status(record),
        "event": record.event, "can_see_private": services.can_see_private(request.user, record),
        "verify_url": verify_url(request, record), "public_key": signing.public_key_hex(),
    })


def pdf(request, code):
    record = _record(code)
    data = drawing.certificate_pdf(json.loads(record.payload), verify_url(request, record), record.key_id)
    response = HttpResponse(data, content_type="application/pdf")
    response["Content-Disposition"] = f'inline; filename="{record.event.slug}-{record.kind}-{record.code}.pdf"'
    return response


def signed_json(request, code):
    record = _record(code)
    response = JsonResponse(services.document(record), json_dumps_params={"ensure_ascii": False, "indent": 2})
    response["Content-Disposition"] = f'attachment; filename="{record.event.slug}-{record.kind}-{record.code}.json"'
    return response


def scores(request, code):
    record = _record(code)
    if not services.can_see_private(request.user, record) or record.kind != Record.Kind.JUDGE:
        raise Http404()
    return render(request, "records/scores.html", {"record": record, "event": record.event,
                                                   "receipt": services.receipt(record)})


def verify(request):
    """Check a code, or paste a signed record to check its signature here."""
    result, pasted = None, ""
    if request.method == "POST":
        code = request.POST.get("code", "").strip()
        if code:
            record = Record.objects.filter(code=code.upper()).first()
            if record:
                return redirect("records:detail", code=record.code)
            result = {"ok": False, "reasons": ["No record has that code. Check it for typos: codes use 2 to 9 and A to Z."]}
        else:
            pasted = request.POST.get("document", "")
            try:
                document = json.loads(pasted)
            except ValueError:
                result = {"ok": False, "reasons": ["That isn't JSON. Paste the whole .json file."]}
            else:
                ok, reasons = signing.check(document, trusted_key=signing.public_key_hex())
                known = Record.objects.filter(code=(document.get("record") or {}).get("code", "")).first() \
                    if isinstance(document, dict) else None
                if ok and known and known.is_revoked:
                    ok, reasons = False, [f"The signature is genuine, but the organizers revoked this record: "
                                          f"{known.revoked_reason}"]
                result = {"ok": ok, "reasons": reasons, "record": document.get("record") if ok else None}
    return render(request, "records/verify.html", {
        "result": result, "pasted": pasted, "public_key": signing.public_key_hex(),
        "key_id": signing.key_id(bytes.fromhex(signing.public_key_hex())),
    })


def verify_code(request, code):
    return redirect("records:detail", code=_record(code).code)


@login_required
def manage(request, slug):
    event, viewer = get_event_for(request, slug)
    require_organizer(viewer)
    if request.method == "POST":
        op = request.POST.get("op")
        try:
            if op == "award":
                award = services.give_award(request.user, event, request.POST.get("prize"),
                                            request.POST.get("project"), request.POST.get("note", ""))
                messages.success(request, f"“{award.prize.name}” goes to “{award.project.title}”.")
            elif op == "unaward":
                services.take_award(request.user, event, request.POST.get("id"))
                messages.success(request, "Award taken back.")
            elif op == "issue":
                made = services.issue(request.user, event, request.POST.getlist("kinds"))
                total = sum(made.values())
                messages.success(request, f"Issued {total} record{'s' if total != 1 else ''}." if total
                                 else "Nothing new to issue: everyone due a record already has one.")
            elif op == "revoke":
                record = get_object_or_404(Record, pk=request.POST.get("id"), event=event)
                services.revoke(request.user, record, request.POST.get("reason", ""))
                messages.success(request, f"Revoked {record.code}. Anyone who checks it now sees why.")
            else:
                return HttpResponseBadRequest("Unknown operation.")
        except services.RecordError as err:
            messages.error(request, err.message)
        return redirect("records:manage", slug=slug)

    results = compute_results(event)
    top = [r for r in results.rows if r.rank][:5] if results.ready else []
    return render(request, "records/manage.html", {
        "event": event, "viewer": viewer, "section": "certificates",
        "prizes": Prize.objects.filter(event=event).select_related("track").prefetch_related("awards__project"),
        "projects": Project.objects.filter(event=event, status="submitted", duplicate_of__isnull=True)
                    .select_related("team", "track").order_by("title"),
        "awards_public": services.awards_public(event), "top": top,
        "records": Record.objects.filter(event=event).select_related("user").order_by("kind", "user__name"),
        "kinds": Record.Kind.choices, "closed": event.is_closed,
    })


def well_known(request):
    """/.well-known/shipshape-records.json: where a program finds the key this
    portal signs certificates and judges' records with, and how to check one."""
    public = signing.public_key_hex()
    response = JsonResponse({
        "issuer": "Shipshape", "algorithm": "Ed25519", "public_key": public,
        "key_id": signing.key_id(bytes.fromhex(public)),
        "signed_bytes": "the record as JSON with sorted keys, no spaces, UTF-8",
        "verify": request.build_absolute_uri(reverse("records:verify")),
        "lookup": request.build_absolute_uri("/api/v1/records/{code}"),
    })
    response["Access-Control-Allow-Origin"] = "*"
    return response

