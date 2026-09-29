"""Import and export pages: bringing a whole event in (Organize), and the
console's Export and import tab. Every import is previewed first."""

import json
import secrets
from pathlib import Path

from django.conf import settings
from django.contrib import messages
from django.http import HttpResponse, HttpResponseBadRequest
from django.shortcuts import redirect, render
from django.utils.text import slugify

from accounts.decorators import event_creator_required, login_required
from events.access import get_event_for, require_organizer
from events.deadline import WindowError

from . import csvimport, export
from .importer import Importer, TransferError, read

MAX_UPLOAD = 200 * 1024 * 1024


def _stash_dir():
    path = Path(settings.DATA_DIR) / "imports"
    path.mkdir(exist_ok=True)
    return path


def _stash(raw):
    token = secrets.token_urlsafe(18)
    path = _stash_dir() / token
    path.write_bytes(raw)
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return token


def _unstash(token, delete=False):
    if not token or "/" in token or "\\" in token or ".." in token:
        return None
    path = _stash_dir() / token
    if not path.is_file():
        return None
    raw = path.read_bytes()
    if delete:
        path.unlink(missing_ok=True)
    return raw


@event_creator_required
def import_event(request):
    """Organize > Import an event: upload, look at the preview, confirm."""
    report, pending = None, request.session.get("pending_import")
    if request.method == "POST":
        op = request.POST.get("op")
        if op == "preview":
            upload = request.FILES.get("file")
            if upload is None:
                messages.error(request, "Choose a fixtures.json, shipshape.json or archive .zip to import.")
                return redirect("transfer:import")
            if upload.size > MAX_UPLOAD:
                messages.error(request, "That file is bigger than 200 MB.")
                return redirect("transfer:import")
            raw = upload.read()
            try:
                kind, data, media = read(upload.name, raw)
                report = Importer(request.user, kind, data, media, request.POST.get("name")).run(preview=True)
            except (TransferError, WindowError) as err:
                messages.error(request, err.message)
                return redirect("transfer:import")
            if pending:
                _unstash(pending.get("token"), delete=True)
            pending = {"token": _stash(raw), "name": request.POST.get("name", ""), "file": upload.name}
            request.session["pending_import"] = pending
        elif op == "commit" and pending:
            raw = _unstash(pending["token"], delete=True)
            request.session.pop("pending_import", None)
            if raw is None:
                messages.error(request, "That upload has expired. Choose the file again.")
                return redirect("transfer:import")
            try:
                kind, data, media = read(pending["file"], raw)
                report = Importer(request.user, kind, data, media, pending["name"]).run()
            except (TransferError, WindowError) as err:
                messages.error(request, err.message)
                return redirect("transfer:import")
            messages.success(request, f"Imported {report['event']['name']}. It's unpublished: look it over, then "
                                      "publish it from Details and dates.")
            return redirect("events:manage", slug=report["event"]["slug"])
        elif op == "cancel":
            if pending:
                _unstash(pending.get("token"), delete=True)
            request.session.pop("pending_import", None)
            return redirect("transfer:import")
        else:
            return HttpResponseBadRequest("Unknown operation.")
    return render(request, "transfer/import.html", {"report": report, "pending": pending if report else None})


def _download(content, filename, content_type):
    response = HttpResponse(content, content_type=content_type)
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    response["Cache-Control"] = "no-store"
    return response


@login_required
def download_archive(request, slug):
    event, viewer = get_event_for(request, slug)
    require_organizer(viewer)
    return _download(export.zip_bytes(event), f"{event.slug}-archive.zip", "application/zip")


@login_required
def download_fixture(request, slug):
    event, viewer = get_event_for(request, slug)
    require_organizer(viewer)
    return _download(json.dumps(export.fixture(event), indent=2, ensure_ascii=False), f"{event.slug}-fixtures.json",
                     "application/json")


@login_required
def manage(request, slug):
    """The console's Export and import tab. CSV imports preview first, then
    apply the same file."""
    event, viewer = get_event_for(request, slug)
    require_organizer(viewer)
    key = f"pending_csv:{event.pk}"
    report, pending = None, request.session.get(key)
    if request.method == "POST":
        op = request.POST.get("op")
        if op in ("preview_teams", "preview_judges"):
            upload = request.FILES.get("file")
            if upload is None:
                messages.error(request, "Choose a .csv file.")
                return redirect("transfer:manage", slug=slug)
            text = upload.read(csvimport.MAX_BYTES + 1).decode("utf-8-sig", "replace")
            what = "teams" if op == "preview_teams" else "judges"
            run = csvimport.import_teams if what == "teams" else csvimport.import_judges
            try:
                report = run(request.user, event, text, preview=True)
            except (TransferError, WindowError) as err:
                messages.error(request, err.message)
                return redirect("transfer:manage", slug=slug)
            pending = {"what": what, "text": text, "file": upload.name}
            request.session[key] = pending
        elif op == "apply" and pending:
            request.session.pop(key, None)
            run = csvimport.import_teams if pending["what"] == "teams" else csvimport.import_judges
            try:
                report = run(request.user, event, pending["text"])
            except (TransferError, WindowError) as err:
                messages.error(request, err.message)
                return redirect("transfer:manage", slug=slug)
            noun = pending["what"][:-1] + ("s" if report["added"] != 1 else "")
            skipped = f", skipped {report['skipped']}" if report["skipped"] else ""
            messages.success(request, f"Added {report['added']} {noun}{skipped}.")
            return redirect("transfer:manage", slug=slug)
        elif op == "cancel":
            request.session.pop(key, None)
            return redirect("transfer:manage", slug=slug)
        else:
            return HttpResponseBadRequest("Unknown operation.")
    return render(request, "transfer/manage.html", {
        "event": event, "viewer": viewer, "section": "transfer", "report": report,
        "pending": pending if report else None, "slug_file": slugify(event.slug),
    })
