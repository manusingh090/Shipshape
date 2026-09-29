"""Prizes given, certificates and judges' records."""

from django.shortcuts import get_object_or_404
from django.urls import reverse

from events.models import Award
from records import services, signing
from records.models import Record

from .. import schema as sc
from .. import serializers as s
from ..core import ApiError, endpoint, json_body
from .events import event_for


# What each endpoint takes and returns (api/schema.py), for the OpenAPI document.
KIND = sc.enum("participant", "award", "judge", "organizer")
SIGNED_PAYLOAD = sc.obj(
    "What is signed. Never changes once issued.", v=sc.integer("The payload's format version."), issuer=sc.string(),
    kind=KIND, title=sc.string(), code=sc.string(), recipient=sc.string(), statement=sc.string(),
    event=sc.obj(slug=sc.string(), name=sc.string(), location=sc.string(), dates=sc.string()),
    details=sc.mapping({}, "Depends on the kind: the team and project, the prize, or for a judge how many projects "
                           "they reviewed and scores_sha256 (never the marks)."),
    issued_at=sc.when())
SIGNED = sc.component("SignedRecord", sc.obj(
    "A signed record file: check it offline with records/verify.py, or POST it to /v1/records/verify.",
    record=SIGNED_PAYLOAD, signature=sc.string("Ed25519, hex, over the record's canonical JSON."),
    public_key=sc.string("Hex."), algorithm=sc.enum("Ed25519"), signed_bytes=sc.string()))
STATUS = sc.obj(valid=sc.boolean(), signature_ok=sc.boolean(), revoked=sc.boolean(), revoked_at=sc.nullable(sc.when()),
                revoked_reason=sc.nullable(sc.string()), problems=sc.array(sc.string()))
RECORD = sc.component("Record", sc.obj(
    code=sc.string(), kind=KIND, recipient=sc.nullable(sc.string()), event=sc.string(), issued_at=sc.when(),
    revoked=sc.boolean(), revoked_reason=sc.nullable(sc.string()), page=sc.url(), pdf=sc.url(),
    signed=sc.optional(SIGNED), status=sc.optional(STATUS)))
AWARD = sc.component("Award", sc.obj(
    id=sc.integer(), prize=sc.obj(id=sc.integer(), name=sc.string()),
    project=sc.obj(id=sc.integer(), title=sc.string(), team=sc.string()), note=sc.nullable(sc.string()), at=sc.when()))
RECEIPT = sc.obj(
    scores=sc.array(sc.obj(project=sc.string(), title=sc.string(), marks=sc.mapping(sc.integer()),
                           submitted_at=sc.when())),
    published_sha256=sc.string(), matches=sc.boolean("The scores hash to the record's fingerprint."),
    changed_since=sc.boolean("The judge's scores have changed since the record was issued."))
VERIFIED = sc.obj(genuine=sc.boolean("Signed by this portal's key, unaltered."), revoked=sc.boolean(),
                  valid=sc.boolean(), problems=sc.array(sc.string()), revoked_reason=sc.nullable(sc.string()))


def record_json(request, r, full=True):
    data = {"code": r.code, "kind": r.kind, "recipient": r.user.display_name if r.user else None,
            "event": r.event.slug, "issued_at": s.when(r.issued_at), "revoked": r.is_revoked,
            "revoked_reason": r.revoked_reason or None,
            "page": request.build_absolute_uri(reverse("records:detail", args=[r.code])),
            "pdf": request.build_absolute_uri(reverse("records:pdf", args=[r.code]))}
    if full:
        data["signed"] = services.document(r)
        data["status"] = services.status(r)
    return data


def award_json(a):
    return {"id": a.pk, "prize": {"id": a.prize_id, "name": a.prize.name}, "project": {"id": a.project_id,
            "title": a.project.title, "team": a.project.team.name}, "note": a.note or None, "at": s.when(a.created_at)}


@endpoint("GET", "events/<slug:slug>/awards", who="anyone once public; organizers always", auth=False,
          summary="Who won what. Private until the event's results date, except to organizers.",
          ui="Event page, Prizes / Console, Certificates",
          returns=sc.obj(public=sc.boolean("Winners are public (the results date has passed)."),
                         results=sc.array(AWARD)))
def awards(request, slug):
    event, viewer = event_for(request, slug)
    rows = (Award.objects.filter(prize__event=event).select_related("prize", "project__team")
            if viewer.is_organizer else services.public_awards(event))
    return {"public": services.awards_public(event), "results": [award_json(a) for a in rows]}


@endpoint("POST", "events/<slug:slug>/awards", who="organizer",
          summary="Give a prize to a project (after the deadline; track prizes stay in their track).",
          ui="Console, Certificates, Give it", body='{"prize": 3, "project": 12, "note": ""}',
          request=sc.obj(prize=sc.integer(), project=sc.integer(), note=sc.optional(sc.string())), returns=AWARD)
def give(request, slug):
    event, _ = event_for(request, slug, organizer=True)
    body = json_body(request)
    return award_json(services.give_award(request.user, event, body.get("prize"), body.get("project"),
                                          body.get("note", "")))


@endpoint("DELETE", "events/<slug:slug>/awards/<int:pk>", who="organizer",
          summary="Take a prize back (revoke its certificates first).", ui="Console, Certificates, Take back",
          returns=sc.obj(taken_back=sc.integer()))
def take(request, slug, pk):
    event, _ = event_for(request, slug, organizer=True)
    services.take_award(request.user, event, pk)
    return {"taken_back": pk}


@endpoint("GET", "events/<slug:slug>/records", who="organizer", summary="Every record issued for the event.",
          ui="Console, Certificates, Issued records",
          returns=sc.results(RECORD))
def event_records(request, slug):
    event, _ = event_for(request, slug, organizer=True)
    return {"results": [record_json(request, r, full=False)
                        for r in Record.objects.filter(event=event).select_related("user", "event")]}


@endpoint("POST", "events/<slug:slug>/records", who="organizer",
          summary="Issue every record of these kinds that's due and not issued yet. Idempotent.",
          ui="Console, Certificates, Issue", body='{"kinds": ["participant", "award", "judge", "organizer"]}',
          request=sc.obj(kinds=sc.optional(sc.array(KIND, "Defaults to every kind."))),
          returns=sc.obj(issued=sc.mapping(sc.integer(), "How many of each kind were issued.")))
def issue(request, slug):
    event, _ = event_for(request, slug, organizer=True)
    kinds = json_body(request).get("kinds") or list(Record.Kind.values)
    if not isinstance(kinds, list):
        raise ApiError(400, "invalid", '"kinds" is a list.')
    return {"issued": services.issue(request.user, event, kinds)}


@endpoint("GET", "me/records", who="signed in", summary="Your certificates and records.",
          ui="Account page, Your certificates and records",
          returns=sc.results(RECORD))
def my_records(request):
    return {"results": [record_json(request, r, full=False)
                        for r in request.user.records.select_related("event", "user")]}


@endpoint("GET", "records/key", who="anyone", auth=False,
          summary="The public key every record is signed with (Ed25519, hex) and its id.", ui="Verify page",
          returns=sc.obj(algorithm=sc.enum("Ed25519"), public_key=sc.string(), key_id=sc.string()))
def key(request):
    public = signing.public_key_hex()
    return {"algorithm": "Ed25519", "public_key": public, "key_id": signing.key_id(bytes.fromhex(public))}


@endpoint("GET", "records/<str:code>", who="anyone with the code", auth=False,
          summary="A record: its signed JSON, and whether it's genuine and unrevoked.", ui="Certificate page",
          returns=RECORD)
def record(request, code):
    r = get_object_or_404(Record.objects.select_related("event", "user"), code=code.upper())
    return record_json(request, r)


@endpoint("GET", "records/<str:code>/scores", who="the judge, or an organizer",
          summary="A judge's record's receipt: the scores behind its fingerprint, and whether they still match.",
          ui="Judge's receipt page",
          returns=RECEIPT)
def receipt(request, code):
    r = get_object_or_404(Record.objects.select_related("event", "user"), code=code.upper())
    if r.kind != Record.Kind.JUDGE or not services.can_see_private(request.user, r):
        raise ApiError(404, "not_found", "No such receipt, or not yours.")
    return services.receipt(r)


@endpoint("POST", "records/<str:code>/revoke", who="organizer", summary="Revoke a record, with a reason.",
          ui="Console, Certificates, Revoke", body='{"reason": "..."}',
          request=sc.obj(reason=sc.string("Shown to anyone who checks the record.")), returns=RECORD)
def revoke(request, code):
    r = get_object_or_404(Record.objects.select_related("event", "user"), code=code.upper())
    event_for(request, r.event.slug, organizer=True)
    services.revoke(request.user, r, json_body(request).get("reason", ""))
    return record_json(request, r)


@endpoint("POST", "records/verify", who="anyone", auth=False,
          summary="Check a signed record's signature against this portal's key, and whether it was revoked here.",
          ui="Verify page, Check the signature", body="the signed record file, as JSON",
          request=SIGNED, returns=VERIFIED)
def verify(request):
    document = json_body(request)
    ok, reasons = signing.check(document, trusted_key=signing.public_key_hex())
    code = (document.get("record") or {}).get("code", "") if isinstance(document.get("record"), dict) else ""
    known = Record.objects.filter(code=code).first()
    revoked = bool(known and known.is_revoked)
    return {"genuine": ok, "revoked": revoked, "valid": ok and not revoked, "problems": reasons,
            "revoked_reason": known.revoked_reason if revoked else None}
