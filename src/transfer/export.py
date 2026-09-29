"""Everything about one event, out of the portal, in formats worth having.

* fixtures.json: the DOGFOOD shared shape (event, tracks, judges, teams,
  projects, scores). Every portal in the hackathon loads this, so it's how an
  organizer moves an event to any of them. Exporting the fixture event gives
  back the fixture itself (a test checks it).
* shipshape.json: everything else too, for moving to another Shipshape or
  keeping a complete copy: descriptions, answers, tags, prizes, awards,
  questions, the rubric's weights, assignments, score history, conflicts,
  the community vote and its ballots, comments, the audit log, the records.
* archive.zip: both, plus every CSV export, the images, each signed record
  as its own file with the key that signed it, and a README explaining it all.

Passwords and secrets never leave: no password hashes, no API tokens, no
webhook secrets, no signing key, no open-link voting token.
"""

import io
import json
import zipfile
from pathlib import Path

from django.conf import settings
from django.utils import timezone

from events.models import Activity, Award, EventRole
from events.timeutil import iso_utc
from judging.exports import STAGES, render_csv
from judging.models import Assignment, AssignmentBatch, Conflict, Criterion, JudgingConfig, Score, ScoreRevision
from projects.models import Comment, Project
from records.models import Record
from records import signing
from teams.models import Membership, Team
from voting.models import Ballot, VotingConfig

FORMAT = "shipshape-archive"
VERSION = 1


def _when(dt):
    return iso_utc(dt) if dt else None


def _ids(event):
    """Stable ids for the shared format: the fixture's own where it gave one."""
    return {
        "event": event.external_id or f"evt_{event.slug}",
        "track": lambda t: t.external_id or f"trk_{t.pk}",
        "team": lambda t: t.external_id or f"tm_{t.pk}",
        "project": lambda p: p.external_id or f"prj_{p.pk}",
        "judge": lambda u: u.external_id or f"jdg_{u.pk}",
    }


def _members(team):
    rows = sorted(team.memberships.select_related("user"), key=lambda m: (m.role != Membership.Role.CAPTAIN, m.joined_at, m.pk))
    return rows


def fixture(event):
    """The event in the DOGFOOD fixtures.json shape."""
    ids = _ids(event)
    judges = (EventRole.objects.filter(event=event, role=EventRole.Role.JUDGE).select_related("user")
              .prefetch_related("tracks").order_by("user__external_id", "user__pk"))
    teams = Team.objects.filter(event=event).order_by("external_id", "pk")
    projects = Project.objects.filter(event=event).select_related("team", "track").order_by("external_id", "pk")
    scores = (Score.objects.filter(event=event, submitted_at__isnull=False).select_related("judge", "project")
              .prefetch_related("items__criterion").order_by("project__external_id", "project_id", "assignment_id"))
    return {
        "event": {"id": ids["event"], "name": event.name, "submissions_close": _when(event.submissions_close_at)},
        "tracks": [{"id": ids["track"](t), "name": t.name} for t in event.tracks.order_by("position", "pk")],
        "judges": [{"id": ids["judge"](r.user), "name": r.user.display_name, "email": r.user.email,
                    "tracks": [ids["track"](t) for t in r.tracks.order_by("position", "pk")]} for r in judges],
        "teams": [{"id": ids["team"](t), "name": t.name, "members": [m.user.email for m in _members(t)]}
                  for t in teams],
        "projects": [{"id": ids["project"](p), "team": ids["team"](p.team),
                      "track": ids["track"](p.track) if p.track else None, "title": p.title, "summary": p.tagline,
                      "repo_url": p.repo_url, "submitted_at": _when(p.submitted_at)} for p in projects],
        "scores": [{"judge": ids["judge"](s.judge), "project": ids["project"](s.project),
                    "criteria": {i.criterion.key: i.value for i in sorted(s.items.all(), key=lambda i: i.criterion.position)},
                    "comment": s.comment} for s in scores],
    }


def archive(event):
    """The whole event, in Shipshape's own format."""
    config = JudgingConfig.objects.filter(event=event).first()
    voting = VotingConfig.objects.filter(event=event).first()
    people = {}

    def who(user):
        if user is None:
            return None
        people.setdefault(user.email, user.display_name)
        return user.email

    data = {
        "format": FORMAT, "version": VERSION, "exported_at": _when(timezone.now()), "exported_by": "Shipshape",
        "event": {k: getattr(event, k) for k in ("slug", "name", "tagline", "description", "location", "timezone",
                                                  "max_team_size", "is_published", "comments_enabled", "gallery_before_deadline", "external_id")}
                 | {k: _when(getattr(event, k)) for k in ("starts_at", "submissions_close_at", "judging_ends_at",
                                                           "results_at")},
        "tracks": [{"id": t.pk, "name": t.name, "description": t.description, "position": t.position,
                    "external_id": t.external_id} for t in event.tracks.order_by("position", "pk")],
        "prizes": [{"id": p.pk, "name": p.name, "value": p.value, "description": p.description, "quantity": p.quantity,
                    "position": p.position, "track": p.track_id} for p in event.prizes.all()],
        "questions": [{"id": q.pk, "prompt": q.prompt, "help_text": q.help_text, "kind": q.kind, "options": q.options,
                       "required": q.required, "is_public": q.is_public, "position": q.position}
                      for q in event.questions.all()],
        "organizers": [who(r.user) for r in EventRole.objects.filter(event=event, role=EventRole.Role.ORGANIZER)
                       .select_related("user")],
        "judges": [{"email": who(r.user), "external_id": r.user.external_id, "all_tracks": r.all_tracks,
                    "tracks": [t.pk for t in r.tracks.all()]}
                   for r in EventRole.objects.filter(event=event, role=EventRole.Role.JUDGE).select_related("user")
                   .prefetch_related("tracks")],
        "teams": [{"id": t.pk, "name": t.name, "external_id": t.external_id, "created_at": _when(t.created_at),
                   "members": [{"email": who(m.user), "role": m.role, "joined_at": _when(m.joined_at)} for m in _members(t)]}
                  for t in Team.objects.filter(event=event).order_by("pk")],
        "projects": [{
            "id": p.pk, "team": p.team_id, "track": p.track_id, "title": p.title, "tagline": p.tagline,
            "description": p.description, "repo_url": p.repo_url, "live_url": p.live_url,
            "demo_video_url": p.demo_video_url, "tags": list(p.tags.values_list("name", flat=True)),
            "status": p.status, "submitted_at": _when(p.submitted_at), "updated_at": _when(p.updated_at),
            "duplicate_of": p.duplicate_of_id, "external_id": p.external_id,
            "answers": {str(a.question_id): a.value for a in p.answers.all()},
            "thumbnail": p.thumbnail or None, "images": [i.path for i in p.images.all()],
        } for p in Project.objects.filter(event=event).order_by("pk")],
        "judging": {
            "config": {"scale_min": config.scale_min, "scale_max": config.scale_max,
                       "reviews_per_project": config.reviews_per_project, "kappa": config.kappa} if config else None,
            "criteria": [{"key": c.key, "label": c.label, "description": c.description, "weight": str(c.weight),
                          "position": c.position} for c in Criterion.objects.filter(event=event)],
            "batches": [{"id": b.pk, "label": b.label, "mode": b.mode, "scope": b.scope, "seed": b.seed,
                         "target_reviews": b.target_reviews, "created_at": _when(b.created_at)}
                        for b in AssignmentBatch.objects.filter(event=event)],
            "assignments": [{"judge": who(a.judge), "project": a.project_id, "batch": a.batch_id,
                             "created_at": _when(a.created_at)}
                            for a in Assignment.objects.filter(event=event).select_related("judge")],
            "scores": [{"judge": who(s.judge), "project": s.project_id, "comment": s.comment, "source": s.source,
                        "submitted_at": _when(s.submitted_at), "updated_at": _when(s.updated_at),
                        "marks": {i.criterion.key: i.value for i in s.items.all()}}
                       for s in Score.objects.filter(event=event).select_related("judge").prefetch_related("items__criterion")],
            "revisions": [{"judge": who(r.judge), "project": r.project_id, "marks": json.loads(r.values_json or "{}"),
                           "comment": r.comment, "submitted": r.submitted, "source": r.source, "at": _when(r.created_at)}
                          for r in ScoreRevision.objects.filter(event=event).select_related("judge").order_by("created_at", "pk")],
            "conflicts": [{"judge": who(c.judge), "project": c.project_id, "reason": c.reason}
                          for c in Conflict.objects.filter(event=event).select_related("judge")],
        },
        "voting": None if voting is None else {
            "config": {k: getattr(voting, k) for k in ("is_enabled", "access", "method", "credits", "max_votes", "order",
                                                       "email_domains")}
                      | {k: _when(getattr(voting, k)) for k in ("opens_at", "closes_at", "results_published_at")},
            "ballots": [{"kind": b.kind, "voter": who(b.user) or b.email or None, "created_at": _when(b.created_at),
                         "updated_at": _when(b.updated_at), "excluded_reason": b.excluded_reason if b.excluded_at else None,
                         "votes": {str(e.project_id): e.votes for e in b.entries.all()}}
                        for b in Ballot.objects.filter(event=event).select_related("user").prefetch_related("entries")],
        },
        "awards": [{"prize": a.prize_id, "project": a.project_id, "note": a.note, "at": _when(a.created_at)}
                   for a in Award.objects.filter(prize__event=event)],
        "comments": [{"project": c.project_id, "author": who(c.author), "body": c.body, "at": _when(c.created_at),
                      "removed_at": _when(c.removed_at), "removed_by": who(c.removed_by), "reason": c.removal_reason}
                     for c in Comment.objects.filter(project__event=event).select_related("author", "removed_by")],
        "records": [{"code": r.code, "kind": r.kind, "recipient": who(r.user), "payload": json.loads(r.payload),
                     "signature": r.signature, "key_id": r.key_id, "revoked_at": _when(r.revoked_at),
                     "revoked_reason": r.revoked_reason or None}
                    for r in Record.objects.filter(event=event).select_related("user")],
        "activity": [{"at": _when(a.created_at), "actor": who(a.actor), "type": a.verb, "detail": a.detail,
                      "team": a.team_id, "project": a.project_id}
                     for a in Activity.objects.filter(event=event).select_related("actor").order_by("created_at", "pk")],
    }
    data["people"] = [{"email": email, "name": name} for email, name in sorted(people.items())]
    return data


README = """Shipshape event archive: {name}
Exported {when}.

  fixtures.json     The event in the DOGFOOD shared shape: event, tracks, judges,
                    teams, projects and scores. Any DOGFOOD portal can load it.
  shipshape.json    Everything, in Shipshape's format (version {version}): details,
                    answers, tags, prizes, awards, the rubric, assignments, score
                    history, conflicts, the community vote and its ballots,
                    comments, the audit log and the certificates. Import it into
                    another Shipshape from Organize > Import an event.
  csv/              Every CSV export, one file per stage, for spreadsheets.
  media/            Project thumbnails and gallery images, as the portal stored them.
  records/          Each certificate and judge's record as a signed file, plus
                    public-key.txt, the key they were signed with. Check one with
                    `python src/records/verify.py records/<code>.json --key <key>`.

Not included: passwords, API tokens, webhook secrets, the signing key, and the
open voting link. People are identified by email.
"""


def zip_bytes(event):
    data = archive(event)
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr("README.txt", README.format(name=event.name, when=data["exported_at"], version=VERSION))
        z.writestr("fixtures.json", json.dumps(fixture(event), indent=2, ensure_ascii=False))
        z.writestr("shipshape.json", json.dumps(data, indent=2, ensure_ascii=False))
        for stage, _, _ in STAGES:
            z.writestr(f"csv/{stage}.csv", render_csv(event, stage))
        root = Path(settings.MEDIA_ROOT)
        paths = {p["thumbnail"] for p in data["projects"] if p["thumbnail"]}
        paths |= {path for p in data["projects"] for path in p["images"]}
        for path in sorted(paths):
            file = (root / path).resolve()
            if root.resolve() in file.parents and file.is_file():
                z.write(file, f"media/{path}")
        z.writestr("records/public-key.txt", signing.public_key_hex() + "\n")
        for r in Record.objects.filter(event=event):
            from records.services import document

            z.writestr(f"records/{r.code}.json", json.dumps(document(r), indent=2, ensure_ascii=False))
    return out.getvalue()
