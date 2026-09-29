"""Awards, certificates and judge records. The console, the API and the
public pages all call these.

Who gets what, once submissions have closed (projects are final):

* participant: everyone on a team whose project is listed in the gallery;
* award: everyone on a team whose project won a prize, once awards are public;
* judge: every judge with at least one submitted score. Their record commits
  to their scores with a SHA-256 of them, without showing the marks, which
  stay private to them and the organizers;
* organizer: everyone who organizes the event.

Issuing is idempotent: running it again only adds what's missing, so an
organizer can issue after the ceremony and again after a late award.
Revoking keeps the record (and its reason) on file; a verifier checking it
here sees it's revoked.
"""

import hashlib
import json

from django.db import IntegrityError, transaction
from django.utils import timezone

from events.deadline import fresh, judging_phase
from events.models import Award, EventRole, Prize, log_activity
from events.timeutil import format_date, iso_utc
from judging.models import Conflict, Score
from projects.models import Project
from teams.models import Membership

from . import signing
from .models import Record

VERSION = 1


class RecordError(Exception):
    status = 400
    code = "record_error"

    def __init__(self, message):
        super().__init__(message)
        self.message = message


# ------------------------------------------------------------------ awards

def awards_public(event, now=None):
    now = now or timezone.now()
    return event.results_at is None or now >= event.results_at


def public_awards(event):
    if not awards_public(event):
        return Award.objects.none()
    return Award.objects.filter(prize__event=event).select_related("prize__track", "project__team")


def give_award(actor, event, prize_id, project_id, note=""):
    with transaction.atomic():
        event = fresh(event)
        if event.phase() != event.Phase.CLOSED:
            raise RecordError("Prizes are given once submissions close, so every project is final.")
        prize = Prize.objects.filter(pk=prize_id, event=event).select_related("track").first()
        project = Project.objects.filter(pk=project_id, event=event, status=Project.Status.SUBMITTED,
                                         duplicate_of__isnull=True).select_related("track").first()
        if prize is None or project is None:
            raise RecordError("Pick one of this event's prizes and a project listed in its gallery.")
        if prize.track_id and project.track_id != prize.track_id:
            raise RecordError(f"“{prize.name}” is for the {prize.track.name} track; that project is in another one.")
        given = Award.objects.filter(prize=prize).count()
        if given >= prize.quantity:
            raise RecordError(f"“{prize.name}” goes to {prize.quantity} winner{'s' if prize.quantity != 1 else ''}, "
                              "and that many have it already. Take it from one first.")
        try:
            with transaction.atomic():
                award = Award.objects.create(prize=prize, project=project, note=(note or "").strip()[:200],
                                             awarded_by=actor)
        except IntegrityError:
            raise RecordError("That project already has that prize.")
    log_activity(event, actor, "award.given", f"gave “{prize.name}” to “{project.title}”",
                 team=project.team, project=project)
    return award


def take_award(actor, event, award_id):
    award = Award.objects.filter(pk=award_id, prize__event=event).select_related("prize", "project__team").first()
    if award is None:
        raise RecordError("No such award.")
    live = Record.objects.filter(event=event, kind=Record.Kind.AWARD, subject=f"award:{award.pk}",
                                 revoked_at__isnull=True).count()
    if live:
        raise RecordError(f"{live} winner's certificate{'s have' if live != 1 else ' has'} been issued for this award. "
                          "Revoke them first, so nobody holds a certificate for a prize that was taken back.")
    award.delete()
    log_activity(event, actor, "award.taken", f"took “{award.prize.name}” back from “{award.project.title}”",
                 team=award.project.team, project=award.project)


# ----------------------------------------------------------------- payloads

def _event_part(event):
    return {"slug": event.slug, "name": event.name, "location": event.location,
            "dates": f"{format_date(event.starts_at, event.tzinfo)} to {format_date(event.submissions_close_at, event.tzinfo)}"}


def score_snapshot(event, judge):
    """The judge's submitted scores, in a fixed order: what the record's
    scores_sha256 commits to."""
    rows = []
    for score in (Score.objects.filter(event=event, judge=judge, submitted_at__isnull=False)
                  .select_related("project").prefetch_related("items__criterion").order_by("project_id")):
        rows.append({"project": score.project.external_id or str(score.project_id), "title": score.project.title,
                     "marks": {i.criterion.key: i.value for i in sorted(score.items.all(), key=lambda i: i.criterion.key)},
                     "submitted_at": iso_utc(score.submitted_at)})
    return rows


def snapshot_digest(rows):
    return hashlib.sha256(signing.canonical({"scores": rows})).hexdigest()


def _candidates(event, kinds):
    """(kind, user, subject, statement, details, private) for everyone due a record."""
    out = []
    if "participant" in kinds or "award" in kinds:
        listed = {p.team_id: p for p in Project.objects.filter(event=event, status=Project.Status.SUBMITTED,
                                                                duplicate_of__isnull=True)}
        members = Membership.objects.filter(event=event, team_id__in=listed).select_related("user", "team")
        if "participant" in kinds:
            for m in members:
                p = listed[m.team_id]
                out.append(("participant", m.user, f"team:{m.team_id}",
                            f"took part in {event.name} with the team {m.team.name}, building “{p.title}”.",
                            {"team": m.team.name, "project": p.title, "track": p.track.name if p.track else None}, None))
        if "award" in kinds:
            by_team = {}
            for m in members:
                by_team.setdefault(m.team_id, []).append(m)
            for award in public_awards(event):
                prize = award.prize
                value = f" ({prize.value})" if prize.value else ""
                for m in by_team.get(award.project.team_id, []):
                    out.append(("award", m.user, f"award:{award.pk}",
                                f"won {prize.name}{value} at {event.name} with the team {m.team.name}, "
                                f"for “{award.project.title}”.",
                                {"prize": prize.name, "value": prize.value or None,
                                 "track": prize.track.name if prize.track else None, "team": m.team.name,
                                 "project": award.project.title, "note": award.note or None}, None))
    if "judge" in kinds:
        for role in EventRole.objects.filter(event=event, role=EventRole.Role.JUDGE).select_related("user") \
                .prefetch_related("tracks"):
            rows = score_snapshot(event, role.user)
            if not rows:
                continue
            tracks = "every track" if role.all_tracks else ", ".join(t.name for t in role.tracks.all()) or "no track"
            n = len(rows)
            conflicts = Conflict.objects.filter(event=event, judge=role.user).count()
            out.append(("judge", role.user, "judge",
                        f"judged {event.name}, submitting {n} review{'s' if n != 1 else ''} "
                        f"({'across ' if role.all_tracks else 'in '}{tracks}).",
                        {"reviews": n, "tracks": tracks, "conflicts_declared": conflicts,
                         "first_score_at": min(r["submitted_at"] for r in rows),
                         "last_score_at": max(r["submitted_at"] for r in rows),
                         "scores_sha256": snapshot_digest(rows)},
                        json.dumps({"scores": rows})))
    if "organizer" in kinds:
        for role in EventRole.objects.filter(event=event, role=EventRole.Role.ORGANIZER).select_related("user"):
            out.append(("organizer", role.user, "organizer", f"organized {event.name}.", {}, None))
    return out


def issue(actor, event, kinds):
    """Issue every record of these kinds that's due and not issued yet.
    Returns {kind: number issued}."""
    kinds = [k for k in kinds if k in Record.Kind.values]
    if not kinds:
        raise RecordError("Pick at least one kind of record to issue.")
    event = type(event).objects.get(pk=event.pk)  # the dates as they are now
    if event.phase() != event.Phase.CLOSED:
        raise RecordError("Records are issued once submissions close, when there's something final to certify.")
    if "judge" in kinds and judging_phase(event) == "not_open":
        raise RecordError("Judges' records come once judging has started.")
    live = set(Record.objects.filter(event=event, revoked_at__isnull=True)
               .values_list("user_id", "kind", "subject"))
    made = {k: 0 for k in kinds}
    now = timezone.now()
    for kind, user, subject, statement, details, private in _candidates(event, kinds):
        if (user.pk, kind, subject) in live:
            continue
        record = Record(event=event, user=user, kind=kind, subject=subject, issued_by=actor, issued_at=now,
                        private=private or "")
        payload = {
            "v": VERSION, "issuer": "Shipshape", "kind": kind, "title": record.get_kind_display(),
            "code": record.code, "recipient": user.display_name, "statement": f"{user.display_name} {statement}",
            "event": _event_part(event), "details": details, "issued_at": iso_utc(now),
        }
        record.signature, record.key_id = signing.sign(payload)
        record.payload = json.dumps(payload, ensure_ascii=False)
        try:
            with transaction.atomic():
                record.save()
        except IntegrityError:
            continue  # issued by a simultaneous run
        live.add((user.pk, kind, subject))
        made[kind] += 1
    total = sum(made.values())
    if total:
        parts = ", ".join(f"{n} {Record.Kind(k).label.lower()}{'s' if n != 1 else ''}" for k, n in made.items() if n)
        log_activity(event, actor, "record.issued", f"issued {parts}")
    return made


def revoke(actor, record, reason):
    reason = (reason or "").strip()[:200]
    if not reason:
        raise RecordError("Say why. The reason is shown to anyone who checks the record.")
    if record.revoked_at:
        raise RecordError("That record is already revoked.")
    record.revoked_at, record.revoked_reason = timezone.now(), reason
    record.save(update_fields=["revoked_at", "revoked_reason"])
    log_activity(record.event, actor, "record.revoked",
                 f"revoked {record.get_kind_display().lower()} {record.code} for {record.user.display_name if record.user else 'someone'}: {reason}")
    return record


# ----------------------------------------------------------------- reading

def document(record):
    return signing.envelope(json.loads(record.payload), record.signature)


def status(record):
    ok, reasons = signing.check(document(record))
    return {"valid": ok and not record.is_revoked, "signature_ok": ok, "revoked": record.is_revoked,
            "revoked_at": iso_utc(record.revoked_at) if record.revoked_at else None,
            "revoked_reason": record.revoked_reason or None, "problems": reasons}


def can_see_private(user, record):
    if not user.is_authenticated:
        return False
    if record.user_id == user.pk:
        return True
    from events.access import Viewer

    return Viewer(user, record.event).is_organizer


def receipt(record):
    """For a judge's record: the scores it committed to, whether they still
    hash to the published value, and whether the judge's scores have changed
    since (for example, revised after the record was issued)."""
    if record.kind != Record.Kind.JUDGE or not record.private:
        return None
    rows = json.loads(record.private)["scores"]
    published = json.loads(record.payload)["details"]["scores_sha256"]
    current = score_snapshot(record.event, record.user) if record.user else []
    return {"scores": rows, "published_sha256": published, "matches": snapshot_digest(rows) == published,
            "changed_since": snapshot_digest(current) != published}
