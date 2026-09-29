"""Bringing an event in: a DOGFOOD fixtures.json, or a Shipshape archive.

An import always makes a new event, so it can never overwrite one that's
running, and it starts unpublished, so nothing is public until the organizer
has looked. Whoever imports it becomes an organizer. Everything happens in
one transaction: a preview runs the whole import and rolls it back, so what
the preview reports is exactly what the real import will do.

People are matched by email, so someone already on the portal keeps their
account. New people get an account with no password; nothing is emailed.

The portal's rules still hold on the way in, with each exception reported:
a judge listed on a team isn't added to it (staff can't compete), a second
listed project from one team is kept as a flagged duplicate (like the
fixture's prj_41), a project handed in after the deadline arrives as a
draft, and a score for someone or something unknown is skipped.

From an archive, these come back too: details and dates, prizes, questions
and answers, tags, links, images (re-checked and re-encoded like any
upload), the rubric's weights, judges' tracks, assignments, scores and their
history, conflicts, the community vote's settings and ballots, awards,
comments and the audit log. Certificates don't: they're statements signed by
the old portal's key and stay valid with that key in the archive's records/
folder. Issue new ones here if needed.
"""

import io
import json
import zipfile
from datetime import timedelta, timezone as dt_timezone
from decimal import Decimal

from django.contrib.auth.hashers import make_password
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.utils.text import slugify

from accounts.models import User, normalize_email
from events.models import Activity, Award, CustomQuestion, Event, EventRole, Prize, Track, log_activity
from judging.models import (Assignment, AssignmentBatch, Conflict, Criterion, CriterionScore, JudgingConfig, Score,
                            ScoreRevision)
from projects.images import ImageError, process_upload
from projects.models import Answer, Comment, Project, ProjectImage, Tag
from teams.models import Membership, Team
from voting.models import Ballot, BallotEntry, VotingConfig

from .export import FORMAT

MAX_ZIP_BYTES = 200 * 1024 * 1024  # uncompressed, all entries together
MAX_ZIP_FILES = 5000
MAX_JSON_BYTES = 50 * 1024 * 1024
HACK_LENGTH = timedelta(hours=72)


class TransferError(Exception):
    """The file can't be imported at all. The message is for the organizer."""

    status = 400
    code = "import_error"

    def __init__(self, message):
        super().__init__(message)
        self.message = message


class _Preview(Exception):
    pass


# ------------------------------------------------------------------ reading

def read(name, raw):
    """(kind, data, media) from an uploaded .json or .zip. kind is "fixture"
    or "archive"; media maps a stored path to its bytes."""
    media = {}
    if raw[:2] == b"PK":
        try:
            z = zipfile.ZipFile(io.BytesIO(raw))
        except zipfile.BadZipFile:
            raise TransferError("That .zip file is damaged.")
        infos = z.infolist()
        if len(infos) > MAX_ZIP_FILES or sum(i.file_size for i in infos) > MAX_ZIP_BYTES:
            raise TransferError("That archive is too big to import (over 200 MB unpacked, or too many files).")
        names = {i.filename for i in infos}
        chosen = "shipshape.json" if "shipshape.json" in names else "fixtures.json" if "fixtures.json" in names else None
        if chosen is None:
            raise TransferError("That .zip has neither a shipshape.json nor a fixtures.json in it.")
        raw_json = z.read(chosen)
        for info in infos:
            path = info.filename
            if path.startswith("media/") and not info.is_dir() and ".." not in path.split("/"):
                media[path[len("media/"):]] = z.read(info)
    else:
        raw_json = raw
    if len(raw_json) > MAX_JSON_BYTES:
        raise TransferError("That file is too big to import.")
    try:
        data = json.loads(raw_json.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError):
        raise TransferError("That isn't valid JSON (or a .zip with JSON in it).")
    if not isinstance(data, dict):
        raise TransferError("That JSON isn't an event.")
    if data.get("format") == FORMAT:
        if data.get("version") != 1:
            raise TransferError(f"That archive is format version {data.get('version')}; this portal reads version 1.")
        return "archive", data, media
    event = data.get("event")
    if isinstance(event, dict) and event.get("name") and event.get("submissions_close"):
        return "fixture", data, media
    raise TransferError("That isn't a DOGFOOD fixtures.json or a Shipshape archive.")


def _dt(value):
    if not value:
        return None
    parsed = parse_datetime(str(value))
    if parsed is None:
        raise TransferError(f"“{value}” isn't a date and time.")
    if timezone.is_naive(parsed):
        parsed = parsed.replace(tzinfo=dt_timezone.utc)
    return parsed


# ---------------------------------------------------------------- importing

class Importer:
    def __init__(self, actor, kind, data, media=None, name=None):
        self.actor, self.kind, self.data, self.media = actor, kind, data, media or {}
        self.name = (name or "").strip()
        self.warnings, self.counts = [], {}
        self.no_password = make_password(None)
        self.people = {}

    def count(self, key, n=1):
        self.counts[key] = self.counts.get(key, 0) + n

    def warn(self, text):
        if len(self.warnings) < 200:
            self.warnings.append(text)

    def run(self, preview=False):
        self.preview = preview
        try:
            with transaction.atomic():
                event = self._event()
                if self.kind == "fixture":
                    self._fixture(event)
                else:
                    self._archive(event)
                log_activity(event, self.actor, "event.imported",
                             f"imported this event from a {'DOGFOOD fixtures.json' if self.kind == 'fixture' else 'Shipshape archive'}")
                self.event = event
                if preview:
                    raise _Preview()
        except _Preview:
            self.event = None
        return self.report()

    def report(self):
        return {"kind": self.kind, "counts": self.counts, "warnings": self.warnings,
                "event": {"slug": self.event.slug, "name": self.event.name} if getattr(self, "event", None) else None}

    # -- helpers

    def person(self, email, name=""):
        email = normalize_email(email)
        if not email or "@" not in email:
            return None
        if email in self.people:
            return self.people[email]
        user = User.objects.filter(email=email).first()
        if user is None:
            local = email.split("@")[0]
            user = User(email=email, name=(name or local.replace(".", " ").replace("_", " ").title())[:120])
            user.password = self.no_password
            user.save()
            self.count("new accounts")
        self.people[email] = user
        return user

    def _slug(self, name):
        base = slugify(name)[:70] or "imported-event"
        slug, n = base, 2
        while Event.objects.filter(slug=slug).exists():
            slug, n = f"{base}-{n}", n + 1
        return slug

    def _event(self):
        if self.kind == "fixture":
            ev = self.data["event"]
            close = _dt(ev.get("submissions_close"))
            fields = {"name": self.name or ev["name"], "starts_at": close - HACK_LENGTH, "submissions_close_at": close,
                      "external_id": str(ev.get("id", ""))[:40], "timezone": "UTC"}
            biggest = max((len(t.get("members", [])) for t in self.data.get("teams", [])), default=4)
            fields["max_team_size"] = max(4, biggest)
        else:
            ev = self.data["event"]
            fields = {k: ev.get(k) for k in ("tagline", "description", "location", "timezone", "max_team_size",
                                              "comments_enabled", "gallery_before_deadline", "external_id") if ev.get(k) is not None}
            fields.update({k: _dt(ev.get(k)) for k in ("starts_at", "submissions_close_at", "judging_ends_at",
                                                        "results_at")})
            fields["name"] = self.name or ev["name"]
        if not fields.get("starts_at") or not fields.get("submissions_close_at") \
                or fields["submissions_close_at"] <= fields["starts_at"]:
            raise TransferError("The event needs a kickoff before its submission deadline.")
        event = Event.objects.create(slug=self._slug(fields["name"]), is_published=False, created_by=self.actor,
                                     **fields)
        EventRole.objects.create(event=event, user=self.actor, role=EventRole.Role.ORGANIZER, added_by=self.actor)
        self.count("events")
        return event

    def _team_member(self, event, team, user, role, joined_at=None):
        if Membership.objects.filter(event=event, user=user).exists():
            self.warn(f"{user.email} is on two teams; kept on the first, not added to {team.name}.")
            return
        if EventRole.objects.filter(event=event, user=user).exists() or user.is_admin:
            self.warn(f"{user.email} is staff here (organizer, judge or admin), so not added to {team.name}.")
            return
        Membership.objects.create(team=team, user=user, event=event, role=role, joined_at=joined_at or event.starts_at)
        self.count("team members")

    def _project(self, event, team, **fields):
        listed = Project.objects.filter(event=event, team=team, duplicate_of__isnull=True).first()
        submitted = fields.get("submitted_at")
        if submitted and submitted >= event.submissions_close_at:
            self.warn(f"“{fields['title']}” was handed in after the deadline; imported as a draft.")
            fields["status"], fields["submitted_at"] = Project.Status.DRAFT, None
        if listed is not None:
            self.warn(f"{team.name} has two projects; “{fields['title']}” is kept as a flagged duplicate of “{listed.title}”.")
        stamp = fields.pop("updated_at", None) or submitted or event.submissions_close_at
        project = Project.objects.create(event=event, team=team, duplicate_of=listed, created_at=stamp,
                                         updated_at=stamp, **fields)
        self.count("projects")
        return project

    def _score(self, event, batch, judge, project, marks, comment, criteria, submitted_at=None, source="import",
               revisions=True):
        if judge is None or project is None:
            self.warn("A score names a judge or project that isn't in the file; skipped.")
            return None
        if Membership.objects.filter(event=event, user=judge, team=project.team).exists():
            self.warn(f"{judge.email} scored their own team's project; skipped.")
            return None
        config = JudgingConfig.objects.get(event=event)
        clean = {}
        for key, value in (marks or {}).items():
            criterion = criteria.get(key)
            if criterion is None or not isinstance(value, int) or not config.scale_min <= value <= config.scale_max:
                self.warn(f"A mark for {key!r} on “{project.title}” is outside the rubric; that score was skipped.")
                return None
            clean[criterion] = value
        assignment, _ = Assignment.objects.get_or_create(event=event, judge=judge, project=project,
                                                         defaults={"batch": batch})
        if Score.objects.filter(assignment=assignment).exists():
            self.warn(f"{judge.email} scored “{project.title}” twice in the file; kept the first.")
            return None
        now = timezone.now()
        score = Score.objects.create(assignment=assignment, event=event, judge=judge, project=project,
                                     comment=comment or "", submitted_at=submitted_at or now,
                                     source=source if source in Score.Source.values else Score.Source.IMPORT,
                                     created_at=submitted_at or now, updated_at=submitted_at or now)
        CriterionScore.objects.bulk_create([CriterionScore(score=score, criterion=c, value=v) for c, v in clean.items()])
        if revisions:
            ScoreRevision.objects.create(event=event, judge=judge, project=project, score=score,
                                         values_json=json.dumps({c.key: v for c, v in clean.items()}),
                                         comment=score.comment, submitted=True, source=Score.Source.IMPORT,
                                         created_at=score.submitted_at)
        self.count("scores")
        return score

    # -- the shared DOGFOOD shape

    def _fixture(self, event):
        d = self.data
        tracks = {}
        for position, t in enumerate(d.get("tracks", []), start=1):
            tracks[t["id"]] = Track.objects.create(event=event, name=str(t["name"])[:80], position=position,
                                                   external_id=str(t["id"])[:40])
            self.count("tracks")
        judges = {}
        for j in d.get("judges", []):
            user = self.person(j.get("email"), j.get("name", ""))
            if user is None:
                self.warn(f"Judge {j.get('id')} has no usable email; skipped.")
                continue
            if user == self.actor or user.is_admin:
                self.warn(f"{user.email} can see every score here, so can't judge; not added as a judge.")
                continue
            if not user.external_id and j.get("id"):
                user.external_id = str(j["id"])[:40]
                user.save(update_fields=["external_id"])
            role = EventRole.objects.create(event=event, user=user, role=EventRole.Role.JUDGE, added_by=self.actor)
            role.tracks.set([tracks[t] for t in j.get("tracks", []) if t in tracks])
            judges[j.get("id")] = user
            self.count("judges")
        teams = {}
        for t in d.get("teams", []):
            team = Team.objects.create(event=event, name=str(t["name"])[:80], external_id=str(t["id"])[:40])
            teams[t["id"]] = team
            self.count("teams")
            for index, email in enumerate(t.get("members", [])):
                user = self.person(email)
                if user:
                    self._team_member(event, team, user,
                                      Membership.Role.CAPTAIN if index == 0 else Membership.Role.MEMBER)
        projects = {}
        for p in d.get("projects", []):
            team = teams.get(p.get("team"))
            if team is None:
                self.warn(f"Project {p.get('id')} names a team that isn't in the file; skipped.")
                continue
            submitted = _dt(p.get("submitted_at"))
            projects[p["id"]] = self._project(
                event, team, track=tracks.get(p.get("track")), title=(p.get("title") or p["id"])[:140],
                tagline=(p.get("summary") or "")[:200], repo_url=p.get("repo_url") or "",
                status=Project.Status.SUBMITTED if submitted else Project.Status.DRAFT, submitted_at=submitted,
                external_id=str(p["id"])[:40])
        scores = d.get("scores") or []
        JudgingConfig.objects.get_or_create(event=event)
        keys = []
        for entry in scores:
            for key in entry.get("criteria", {}):
                if key not in keys:
                    keys.append(key)
        criteria = {key: Criterion.objects.create(event=event, key=slugify(key)[:40] or f"c{n}",
                                                  label=key.replace("_", " ").capitalize()[:80], weight=1, position=n)
                    for n, key in enumerate(keys, start=1)}
        if scores:
            batch = AssignmentBatch.objects.create(event=event, label="Imported", mode=AssignmentBatch.Mode.IMPORT,
                                                   target_reviews=3, scope="scores from the imported file")
            for s in scores:
                self._score(event, batch, judges.get(s.get("judge")), projects.get(s.get("project")),
                            s.get("criteria"), s.get("comment", ""), criteria)

    # -- a whole Shipshape archive

    def _archive(self, event):
        d = self.data
        for p in d.get("people", []):
            self.person(p.get("email"), p.get("name", ""))
        tracks = {}
        for t in d.get("tracks", []):
            tracks[t["id"]] = Track.objects.create(event=event, name=t["name"][:80], description=t.get("description", ""),
                                                   position=t.get("position", 0), external_id=t.get("external_id", ""))
            self.count("tracks")
        prizes = {}
        for p in d.get("prizes", []):
            prizes[p["id"]] = Prize.objects.create(event=event, name=p["name"], value=p.get("value", ""),
                                                   description=p.get("description", ""), quantity=p.get("quantity", 1),
                                                   position=p.get("position", 0), track=tracks.get(p.get("track")))
            self.count("prizes")
        questions = {}
        for q in d.get("questions", []):
            questions[str(q["id"])] = CustomQuestion.objects.create(
                event=event, prompt=q["prompt"], help_text=q.get("help_text", ""), kind=q.get("kind", "short"),
                options=q.get("options", ""), required=q.get("required", False), is_public=q.get("is_public", True),
                position=q.get("position", 0))
            self.count("questions")
        for email in d.get("organizers", []):
            user = self.person(email)
            if user and user != self.actor:
                EventRole.objects.get_or_create(event=event, user=user, role=EventRole.Role.ORGANIZER,
                                                defaults={"added_by": self.actor})
                self.count("organizers")
        for j in d.get("judges", []):
            user = self.person(j.get("email"))
            if user is None:
                continue
            if EventRole.objects.filter(event=event, user=user, role=EventRole.Role.ORGANIZER).exists() or user.is_admin:
                self.warn(f"{user.email} can see every score here, so can't judge; not added as a judge.")
                continue
            role = EventRole.objects.create(event=event, user=user, role=EventRole.Role.JUDGE,
                                            all_tracks=j.get("all_tracks", False), added_by=self.actor)
            role.tracks.set([tracks[t] for t in j.get("tracks", []) if t in tracks])
            self.count("judges")
        teams = {}
        for t in d.get("teams", []):
            team = Team.objects.create(event=event, name=t["name"][:80], external_id=t.get("external_id", ""),
                                       created_at=_dt(t.get("created_at")) or timezone.now())
            teams[t["id"]] = team
            self.count("teams")
            for m in t.get("members", []):
                user = self.person(m.get("email"))
                if user:
                    self._team_member(event, team, user, m.get("role", Membership.Role.MEMBER), _dt(m.get("joined_at")))
        projects = {}
        rows = sorted(d.get("projects", []), key=lambda p: (p.get("duplicate_of") is not None, p["id"]))
        for p in rows:
            team = teams.get(p.get("team"))
            if team is None:
                self.warn(f"Project “{p.get('title')}” names a team that isn't in the file; skipped.")
                continue
            project = self._project(
                event, team, track=tracks.get(p.get("track")), title=p["title"][:140], tagline=p.get("tagline", "")[:200],
                description=p.get("description", ""), repo_url=p.get("repo_url", ""), live_url=p.get("live_url", ""),
                demo_video_url=p.get("demo_video_url", ""), status=p.get("status", Project.Status.DRAFT),
                submitted_at=_dt(p.get("submitted_at")), updated_at=_dt(p.get("updated_at")),
                external_id=p.get("external_id", ""))
            projects[p["id"]] = project
            for name in p.get("tags", []):
                tag, _ = Tag.objects.get_or_create(name=name.strip().lower()[:40])
                project.tags.add(tag)
            for qid, value in (p.get("answers") or {}).items():
                if qid in questions and value:
                    Answer.objects.create(project=project, question=questions[qid], value=value)
            self._media(project, p)
        # Keep the file's own choice of which duplicate is listed.
        for p in rows:
            if p.get("duplicate_of") and p["id"] in projects and p["duplicate_of"] in projects:
                Project.objects.filter(pk=projects[p["id"]].pk).update(duplicate_of=projects[p["duplicate_of"]])

        j = d.get("judging") or {}
        cfg = j.get("config") or {}
        JudgingConfig.objects.update_or_create(event=event, defaults={k: cfg[k] for k in (
            "scale_min", "scale_max", "reviews_per_project", "kappa") if k in cfg})
        criteria = {c["key"]: Criterion.objects.create(event=event, key=c["key"], label=c["label"],
                                                       description=c.get("description", ""),
                                                       weight=Decimal(str(c.get("weight", 1))), position=c.get("position", 0))
                    for c in j.get("criteria", [])}
        batches = {b["id"]: AssignmentBatch.objects.create(
            event=event, label=b["label"][:80], mode=b.get("mode", AssignmentBatch.Mode.IMPORT), scope=b.get("scope", ""),
            seed=b.get("seed"), target_reviews=b.get("target_reviews", 3), created_at=_dt(b.get("created_at")) or timezone.now())
            for b in j.get("batches", [])}
        imported = AssignmentBatch.objects.create(event=event, label="Imported", mode=AssignmentBatch.Mode.IMPORT,
                                                  target_reviews=3, scope="assignments from the imported archive")
        for a in j.get("assignments", []):
            judge, project = self.person(a.get("judge")), projects.get(a.get("project"))
            if judge and project and EventRole.objects.filter(event=event, user=judge, role=EventRole.Role.JUDGE).exists():
                Assignment.objects.get_or_create(event=event, judge=judge, project=project,
                                                 defaults={"batch": batches.get(a.get("batch"), imported),
                                                           "created_at": _dt(a.get("created_at")) or timezone.now()})
        for s in j.get("scores", []):
            if not s.get("submitted_at"):
                continue  # drafts stay with the judge who wrote them
            self._score(event, imported, self.person(s.get("judge")), projects.get(s.get("project")), s.get("marks"),
                        s.get("comment", ""), criteria, _dt(s.get("submitted_at")), s.get("source", "import"),
                        revisions=False)
        for r in j.get("revisions", []):
            judge, project = self.person(r.get("judge")), projects.get(r.get("project"))
            score = Score.objects.filter(event=event, judge=judge, project=project).first() if judge and project else None
            if score:
                ScoreRevision.objects.create(event=event, judge=judge, project=project, score=score,
                                             values_json=json.dumps(r.get("marks") or {}), comment=r.get("comment", ""),
                                             submitted=r.get("submitted", True), source=Score.Source.IMPORT,
                                             created_at=_dt(r.get("at")) or timezone.now())
        for c in j.get("conflicts", []):
            judge, project = self.person(c.get("judge")), projects.get(c.get("project"))
            if judge and project:
                Conflict.objects.get_or_create(event=event, judge=judge, project=project,
                                               defaults={"reason": c.get("reason", ""), "declared_by": self.actor})

        v = d.get("voting")
        if v:
            cfg = v.get("config") or {}
            VotingConfig.objects.create(event=event, **{k: cfg[k] for k in (
                "is_enabled", "access", "method", "credits", "max_votes", "order", "email_domains") if k in cfg},
                opens_at=_dt(cfg.get("opens_at")), closes_at=_dt(cfg.get("closes_at")),
                results_published_at=_dt(cfg.get("results_published_at")))
            for b in v.get("ballots", []):
                voter = b.get("voter") or ""
                user = self.person(voter) if b.get("kind") in ("account", "link") and "@" in voter else None
                ballot = Ballot.objects.create(event=event, kind=b.get("kind", "link"), user=user,
                                               email=voter if b.get("kind") == "email" else "",
                                               created_at=_dt(b.get("created_at")) or timezone.now(),
                                               updated_at=_dt(b.get("updated_at")) or timezone.now(),
                                               excluded_at=timezone.now() if b.get("excluded_reason") else None,
                                               excluded_reason=b.get("excluded_reason") or "")
                BallotEntry.objects.bulk_create([BallotEntry(ballot=ballot, project=projects[int(pid)], votes=n)
                                                 for pid, n in (b.get("votes") or {}).items()
                                                 if int(pid) in projects and n > 0])
                self.count("ballots")
        for a in d.get("awards", []):
            if a.get("prize") in prizes and a.get("project") in projects:
                Award.objects.create(prize=prizes[a["prize"]], project=projects[a["project"]], note=a.get("note", ""),
                                     awarded_by=self.actor, created_at=_dt(a.get("at")) or timezone.now())
                self.count("awards")
        for c in d.get("comments", []):
            author, project = self.person(c.get("author")), projects.get(c.get("project"))
            if author and project:
                Comment.objects.create(project=project, author=author, body=c.get("body", "")[:2000],
                                       created_at=_dt(c.get("at")) or timezone.now(), removed_at=_dt(c.get("removed_at")),
                                       removed_by=self.person(c.get("removed_by")) if c.get("removed_by") else None,
                                       removal_reason=c.get("reason") or "")
                self.count("comments")
        entries = []
        for a in d.get("activity", []):
            entries.append(Activity(event=event, actor=self.person(a["actor"]) if a.get("actor") else None,
                                    verb=a.get("type", "")[:40], detail=(a.get("detail") or "")[:400],
                                    team=teams.get(a.get("team")), project=projects.get(a.get("project")),
                                    created_at=_dt(a.get("at")) or timezone.now()))
        Activity.objects.bulk_create(entries)  # history, not news: no webhooks for it
        self.count("log entries", len(entries))
        if d.get("records"):
            self.warn(f"{len(d['records'])} certificates weren't imported: they're signed by the old portal's key and "
                      "stay valid with it (records/ in the archive). Issue new ones here if you need them.")

    def _media(self, project, p):
        def load(path):
            data = self.media.get(path)
            if data is None:
                return None
            if self.preview:  # the rollback can't take back files, so a preview writes none
                return (path, 0, 0)
            try:
                return process_upload(SimpleUploadedFile(path.rsplit("/", 1)[-1], data), f"projects/{project.team_id}")
            except ImageError as err:
                self.warn(f"An image of “{project.title}” wasn't imported: {err}")
                return None

        if p.get("thumbnail"):
            stored = load(p["thumbnail"])
            if stored:
                if not self.preview:
                    Project.objects.filter(pk=project.pk).update(thumbnail=stored[0])
                self.count("images")
        for position, path in enumerate(p.get("images", [])):
            stored = load(path)
            if stored:
                if not self.preview:
                    ProjectImage.objects.create(project=project, path=stored[0], width=stored[1], height=stored[2],
                                                position=position)
                self.count("images")
