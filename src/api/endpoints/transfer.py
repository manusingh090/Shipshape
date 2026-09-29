"""Bulk import and export: a whole event out (archive or fixtures.json), a
whole event in, and teams or judges from a spreadsheet. Imports preview when
asked to, exactly like the pages."""

import json

from django.http import HttpResponse

from transfer import csvimport, export
from transfer.importer import Importer, TransferError, read
from transfer.views import MAX_UPLOAD

from .. import schema as sc
from ..core import ApiError, endpoint, json_body
from .events import event_for


# What each endpoint takes and returns (api/schema.py), for the OpenAPI document.
FIXTURE = sc.component("DogfoodFixture", sc.obj(
    "The shared DOGFOOD 2026 fixtures.json shape, which every portal in the hackathon loads. Ids are strings "
    "(evt_01, trk_01...); people are identified by email.",
    event=sc.obj(id=sc.string(), name=sc.string(), submissions_close=sc.when()),
    tracks=sc.array(sc.obj(id=sc.string(), name=sc.string())),
    judges=sc.array(sc.obj(id=sc.string(), name=sc.string(), email=sc.string(), tracks=sc.array(sc.string()))),
    teams=sc.array(sc.obj(id=sc.string(), name=sc.string(), members=sc.array(sc.string(), "Emails, captain first."))),
    projects=sc.array(sc.obj(id=sc.string(), team=sc.string(), track=sc.nullable(sc.string()), title=sc.string(),
                             summary=sc.string(), repo_url=sc.string(), submitted_at=sc.nullable(sc.when()))),
    scores=sc.array(sc.obj(judge=sc.string(), project=sc.string(), criteria=sc.mapping(sc.integer()),
                           comment=sc.string()))))
ARCHIVE = sc.obj("A Shipshape archive (shipshape.json): the whole event. README.txt in every archive describes it.",
                 _open=True, format=sc.enum("shipshape-archive"), version=sc.enum(1))
IMPORT_BODY = sc.obj(data=sc.any_of(FIXTURE, ARCHIVE), name=sc.optional(sc.string("A name for the new event.")),
                     preview=sc.optional(sc.boolean("Run the whole import and roll it back, to see what it would do.")))
IMPORT_UPLOAD = sc.obj(file=sc.string("fixtures.json, shipshape.json or an archive .zip (at most 200 MB).",
                                      format="binary"),
                       name=sc.optional(sc.string()), preview=sc.optional(sc.string("true to preview.")))
IMPORTED = sc.obj(preview=sc.boolean(), kind=sc.enum("fixture", "archive"),
                  counts=sc.mapping(sc.integer(), "What was (or would be) created, by kind."),
                  warnings=sc.array(sc.string(), "Everything that went in differently, and why: the portal's rules "
                                                 "hold over the file's."),
                  event=sc.nullable(sc.obj(slug=sc.string(), name=sc.string(), _description="The new event; "
                                           "null in a preview.")))
CSV_BODY = sc.obj(csv=sc.string("The spreadsheet, as CSV text (at most 2 MB, 5000 rows)."),
                  preview=sc.optional(sc.boolean()))
CSV_UPLOAD = sc.obj(file=sc.string("A .csv file.", format="binary"), preview=sc.optional(sc.string()))
CSV_IMPORTED = sc.obj(preview=sc.boolean(), added=sc.integer(), skipped=sc.integer(), new_accounts=sc.integer(),
                      problems=sc.array(sc.string(), "Each skipped row and why (the first 200)."))


def _truthy(value):
    return value is True or str(value).lower() in ("1", "true", "yes")


def _download(content, filename, content_type):
    response = HttpResponse(content, content_type=content_type)
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


def _input(request, field):
    """(file name, raw bytes, options) from a multipart upload (with a token)
    or a JSON body."""
    if request.content_type == "multipart/form-data":
        upload = request.FILES.get("file")
        if upload is None:
            raise ApiError(400, "invalid", "Send the file as multipart field `file`.")
        if upload.size > MAX_UPLOAD:
            raise ApiError(413, "too_large", "That file is bigger than 200 MB.")
        return upload.name, upload.read(), request.POST
    body = json_body(request)
    if field not in body:
        raise ApiError(400, "invalid", f"Send `{field}` in the JSON body, or the file as multipart field `file`.",
                       fields={field: ["This field is required."]})
    return None, body[field], body


def _refused(err):
    return ApiError(400, "import_refused", err.message)


@endpoint("GET", "events/<slug:slug>/export/archive.zip", who="organizer",
          summary="The whole event as a .zip: fixtures.json, shipshape.json, every CSV, images, signed records and a README.",
          ui="Console, Export and import, Download .zip",
          produces="application/zip")
def archive(request, slug):
    event, _ = event_for(request, slug, organizer=True)
    return _download(export.zip_bytes(event), f"{event.slug}-archive.zip", "application/zip")


@endpoint("GET", "events/<slug:slug>/export/fixtures.json", who="organizer",
          summary="The event in the shared DOGFOOD fixtures.json shape.",
          ui="Console, Export and import, Download .json",
          returns=FIXTURE)
def fixture(request, slug):
    event, _ = event_for(request, slug, organizer=True)
    return _download(json.dumps(export.fixture(event), indent=2, ensure_ascii=False),
                     f"{event.slug}-fixtures.json", "application/json")


@endpoint("POST", "imports", who="platform organizer or admin",
          summary="Import a whole event from fixtures.json, shipshape.json or an archive .zip (multipart `file`, with a "
                  "token), or from JSON `data`. It becomes a new unpublished event you organize. `preview` saves nothing.",
          ui="Organize, Import an event",
          body='{"data": {...fixtures.json or shipshape.json...}, "name": "optional new name", "preview": true}',
          request=IMPORT_BODY, multipart=IMPORT_UPLOAD, returns=IMPORTED)
def import_event(request):
    if not request.user.can_create_events:
        raise ApiError(403, "forbidden", "Importing an event needs the platform organizer role.")
    name, raw, opts = _input(request, "data")
    if name is None:
        if not isinstance(raw, dict):
            raise ApiError(400, "invalid", "`data` must be a JSON object.", fields={"data": ["Send an object."]})
        name, raw = "upload.json", json.dumps(raw).encode("utf-8")
    preview = _truthy(opts.get("preview"))
    try:
        kind, data, media = read(name, raw)
        report = Importer(request.user, kind, data, media, opts.get("name")).run(preview=preview)
    except TransferError as err:
        raise _refused(err)
    return {"preview": preview, **report}


def _csv(request, slug, run):
    event, _ = event_for(request, slug, organizer=True)
    name, raw, opts = _input(request, "csv")
    if isinstance(raw, bytes):
        raw = raw[:csvimport.MAX_BYTES + 1].decode("utf-8-sig", "replace")
    if not isinstance(raw, str):
        raise ApiError(400, "invalid", "`csv` must be the file's text.", fields={"csv": ["Send a string."]})
    preview = _truthy(opts.get("preview"))
    try:
        report = run(request.user, event, raw, preview=preview)
    except TransferError as err:
        raise _refused(err)
    return {"preview": preview, **report}


@endpoint("POST", "events/<slug:slug>/import/teams", who="organizer",
          summary="Add teams and members from a CSV (columns team, member_email, optional member_name and role). "
                  "Rows that break a rule are skipped and listed.",
          ui="Console, Export and import, Add teams from a spreadsheet",
          body='{"csv": "team,member_email\\nRiver Crew,asha@example.org", "preview": true}',
          request=CSV_BODY, multipart=CSV_UPLOAD, returns=CSV_IMPORTED)
def import_teams(request, slug):
    return _csv(request, slug, csvimport.import_teams)


@endpoint("POST", "events/<slug:slug>/import/judges", who="organizer",
          summary="Add judges from a CSV (columns email, optional name, tracks and all_tracks).",
          ui="Console, Export and import, Add judges from a spreadsheet",
          body='{"csv": "email,tracks\\nlee@example.org,Health", "preview": true}',
          request=CSV_BODY, multipart=CSV_UPLOAD, returns=CSV_IMPORTED)
def import_judges(request, slug):
    return _csv(request, slug, csvimport.import_judges)
