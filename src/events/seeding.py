"""Load fixtures.json, plus demo accounts when DEMO_MODE is on.

Safe to run on every boot: records are matched by their fixture id and only
created when missing, so edits made in the portal are never overwritten.

How the fixture's awkward cases land:
* prj_41 repeats prj_07 (same team, title and repo). It is imported with
  duplicate_of pointing at prj_07, which keeps it out of the gallery and
  shows it to organizers as something to resolve.
* Two teams are both called StillTrail. Team names are not unique; ids are.
* Thirteen teams have a single member. That is allowed.
* The fixture only gives a deadline, so kickoff is set 72 hours earlier
  (the length of a DOGFOOD weekend). The earliest fixture submission is
  still after that kickoff.
* A project submitted after the deadline would be imported as a draft and
  reported. The current fixture has none.
* Every fixture score becomes a completed assignment in one "imported" batch,
  scored against a rubric of the fixture's three criteria at equal weight.
  Projects end up with two to five reviews, as in the file. jdg_07 (every
  mark a 4) and the two single-score judges come through unchanged; the
  normalization, not the importer, deals with them.
* In demo mode the seed then runs, once, the batch an organizer would run
  first: top every project up to three reviews. Eight fixture projects only
  have two (the unfinished batches), so eight new reviews are assigned and
  left pending, which is what the progress dashboard shows as not started.
* Demo mode also sets up a community vote on each event, with no ballots:
  the fixture event's is open now and email-gated (the demo shows the link
  that would have been mailed), the demo event's opens at its deadline and
  needs an account.
* Demo mode issues the fixture event's certificates and judges' records
  (signed, like an organizer would), but gives no prizes: the fixture has none.
* Demo mode also adds one webhook to the fixture event, pointed at the
  portal's own signature-checking receiver, so deliveries can be watched
  offline.
"""

import json
import re
from datetime import timedelta
from pathlib import Path

from django.contrib.auth import BACKEND_SESSION_KEY, HASH_SESSION_KEY, SESSION_KEY
from django.contrib.auth.hashers import make_password
from django.contrib.sessions.backends.db import SessionStore
from django.contrib.sessions.models import Session
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.utils.text import slugify

from accounts.demo import DEMO_LOGINS
from accounts.models import User, UserSession, normalize_email
from judging.models import (Assignment, AssignmentBatch, Criterion, CriterionScore, JudgingConfig, Score,
                            ScoreRevision)
from projects.models import Project
from teams.models import Membership, Team
from voting.models import VotingConfig

from .models import CustomQuestion, Event, EventRole, Prize, Track

HACK_LENGTH = timedelta(hours=72)
DEMO_EVENT_ID = "demo_autumn"
IMPORT_BATCH = "Imported from fixtures.json"
TOPUP_BATCH = "Top-up to 3 reviews (demo seed)"
DEMO_WEBHOOK = "Built-in test receiver (demo seed)"
CRITERION_NOTES = {
    "functionality": "Does it work? Can you try the main idea from start to finish?",
    "quality": "Is it well made: code, design, documentation, the details?",
    "innovation": "Is it a new idea, or a familiar one done in a genuinely new way?",
}


def load_fixture(path):
    with open(Path(path), encoding="utf-8") as handle:
        return json.load(handle)


def name_from_email(email):
    """priya1@example.org -> Priya, member7_2@example.org -> Member 7-2"""
    local = email.split("@")[0]
    letters = "".join(ch for ch in local if ch.isalpha())
    numbers = re.findall(r"\d+", local)
    if letters.lower() == "member" and numbers:
        return "Member " + "-".join(numbers)
    return letters.capitalize() or local


def unique_slug(base):
    base = slugify(base)[:70] or "event"
    slug, n = base, 2
    while Event.objects.filter(slug=slug).exists():
        slug = f"{base}-{n}"
        n += 1
    return slug


class Seeder:
    def __init__(self, demo=False, password=None, out=print):
        self.demo = demo
        self.out = out
        # One hash for every seeded account: hashing a hundred passwords at
        # full strength would add a minute to every first boot.
        self.password_hash = make_password(password) if demo and password else make_password(None)
        self.warnings = []

    # --------------------------------------------------------------- helpers

    def user(self, email, name, external_id="", role=User.Role.MEMBER):
        email = normalize_email(email)
        user = User.objects.filter(email=email).first()
        if user:
            return user
        user = User(email=email, name=name, external_id=external_id, platform_role=role)
        user.password = self.password_hash
        user.save()
        return user

    # ------------------------------------------------------------- fixtures

    def seed_fixture(self, fixture):
        ev = fixture["event"]
        close = parse_datetime(ev["submissions_close"])
        teams_data = fixture.get("teams", [])
        biggest = max((len(t.get("members", [])) for t in teams_data), default=4)

        event = Event.objects.filter(external_id=ev["id"]).first()
        if event is None:
            event = Event.objects.create(
                external_id=ev["id"],
                slug=unique_slug(ev["name"]),
                name=ev["name"],
                tagline="The shared DOGFOOD fixture event: forty teams, eight tracks, one deadline.",
                description=(
                    "Imported from `fixtures.json`, the data every DOGFOOD 2026 portal loads.\n\n"
                    "The deadline has passed, so this event is closed: the gallery is public, "
                    "rosters are locked and no project can be created or changed."
                ),
                timezone="UTC",
                starts_at=close - HACK_LENGTH,
                submissions_close_at=close,
                max_team_size=max(biggest, 4),
                is_published=True,
            )

        tracks = {}
        for position, t in enumerate(fixture.get("tracks", []), start=1):
            track = Track.objects.filter(event=event, external_id=t["id"]).first()
            if track is None:
                track = Track.objects.create(event=event, external_id=t["id"], name=t["name"], position=position)
            tracks[t["id"]] = track

        for j in fixture.get("judges", []):
            judge = self.user(j["email"], j["name"], external_id=j["id"])
            role, created = EventRole.objects.get_or_create(event=event, user=judge, role=EventRole.Role.JUDGE)
            if created:
                role.tracks.set([tracks[t] for t in j.get("tracks", []) if t in tracks])

        teams = {}
        for t in teams_data:
            team = Team.objects.filter(event=event, external_id=t["id"]).first()
            if team is None:
                team = Team.objects.create(event=event, external_id=t["id"], name=t["name"])
            teams[t["id"]] = team
            for index, email in enumerate(t.get("members", [])):
                member = self.user(email, name_from_email(email))
                if Membership.objects.filter(event=event, user=member).exists():
                    continue
                if EventRole.objects.filter(event=event, user=member).exists():
                    self.warnings.append(f"{email} is staff in {event.name}; not added to {team.name}")
                    continue
                Membership.objects.create(
                    team=team, user=member, event=event, joined_at=event.starts_at,
                    role=Membership.Role.CAPTAIN if index == 0 else Membership.Role.MEMBER,
                )

        for p in fixture.get("projects", []):
            if Project.objects.filter(event=event, external_id=p["id"]).exists():
                continue
            team = teams.get(p.get("team"))
            if team is None:
                self.warnings.append(f"{p['id']} names unknown team {p.get('team')!r}; skipped")
                continue
            submitted_at = parse_datetime(p["submitted_at"]) if p.get("submitted_at") else None
            late = submitted_at is not None and submitted_at >= close
            if late:
                self.warnings.append(f"{p['id']} was submitted after the deadline; imported as a draft")
            listed = Project.objects.filter(event=event, team=team, duplicate_of__isnull=True).first()
            stamp = submitted_at or close
            Project.objects.create(
                event=event,
                team=team,
                track=tracks.get(p.get("track")),
                title=p.get("title", "").strip() or p["id"],
                tagline=p.get("summary", ""),
                repo_url=p.get("repo_url", ""),
                status=Project.Status.DRAFT if (late or not submitted_at) else Project.Status.SUBMITTED,
                submitted_at=None if late else submitted_at,
                created_at=stamp,
                updated_at=stamp,
                external_id=p["id"],
                duplicate_of=listed,
            )
        return event

    # ---------------------------------------------------------------- judging

    def seed_judging(self, event, fixture):
        scores = fixture.get("scores") or []
        JudgingConfig.objects.get_or_create(event=event)
        if not scores:
            return
        keys = []
        for entry in scores:
            for key in entry.get("criteria", {}):
                if key not in keys:
                    keys.append(key)
        criteria = {}
        for position, key in enumerate(keys, start=1):
            criteria[key], _ = Criterion.objects.get_or_create(
                event=event, key=slugify(key)[:40],
                defaults={"label": key.replace("_", " ").capitalize(), "weight": 1, "position": position,
                          "description": CRITERION_NOTES.get(key, "")},
            )

        judges = {u.external_id: u for u in User.objects.filter(external_id__in={e["judge"] for e in scores})}
        projects = {p.external_id: p for p in Project.objects.filter(event=event)}
        existing = set(Assignment.objects.filter(event=event).values_list("judge_id", "project_id"))
        batch = AssignmentBatch.objects.filter(event=event, label=IMPORT_BATCH).first()
        if batch is None:
            batch = AssignmentBatch.objects.create(
                event=event, label=IMPORT_BATCH, mode=AssignmentBatch.Mode.IMPORT, target_reviews=3,
                scope="the scores in fixtures.json, one review each",
            )
        now = timezone.now()
        for entry in scores:
            judge, project = judges.get(entry["judge"]), projects.get(entry["project"])
            if judge is None or project is None:
                self.warnings.append(f"score {entry['judge']} on {entry['project']} names someone or something unknown")
                continue
            if (judge.pk, project.pk) in existing:
                continue
            marks = entry.get("criteria", {})
            if any(not isinstance(v, int) or not 1 <= v <= 5 for v in marks.values()):
                self.warnings.append(f"score {entry['judge']} on {entry['project']} has marks outside 1 to 5; skipped")
                continue
            assignment = Assignment.objects.create(event=event, judge=judge, project=project, batch=batch,
                                                   created_at=now)
            score = Score.objects.create(
                assignment=assignment, event=event, judge=judge, project=project, comment=entry.get("comment", ""),
                submitted_at=now, source=Score.Source.IMPORT, created_at=now, updated_at=now,
            )
            CriterionScore.objects.bulk_create(
                [CriterionScore(score=score, criterion=criteria[k], value=v) for k, v in marks.items()]
            )
            ScoreRevision.objects.create(
                event=event, judge=judge, project=project, score=score, values_json=json.dumps(marks),
                comment=score.comment, submitted=True, source=Score.Source.IMPORT, created_at=now,
            )
            existing.add((judge.pk, project.pk))

    def seed_topup(self, event, organizer):
        """The first batch an organizer runs: fill every project up to three
        reviews. Deterministic seed, so every demo portal gets the same one."""
        from judging.assigning import run_batch

        if AssignmentBatch.objects.filter(event=event, label=TOPUP_BATCH).exists():
            return
        run_batch(organizer, event, label=TOPUP_BATCH, target=3, seed=2026,
                  scope="every project below target, every judge, up to 3 reviews each")

    # ----------------------------------------------------------------- demo

    def seed_people(self, fixture_event):
        organizer = self.user("organizer@example.org", "Rosa Mendel", role=User.Role.ORGANIZER)
        self.user("admin@example.org", "Kit Adeyemi", role=User.Role.ADMIN)
        self.user("tom@example.org", "Tom Baptiste")
        if fixture_event is not None:
            EventRole.objects.get_or_create(
                event=fixture_event, user=organizer, role=EventRole.Role.ORGANIZER
            )
        return organizer

    def seed_demo_event(self, organizer):
        if Event.objects.filter(external_id=DEMO_EVENT_ID).exists():
            return
        now = timezone.now().replace(minute=0, second=0, microsecond=0)
        close = now + timedelta(days=3)
        event = Event.objects.create(
            external_id=DEMO_EVENT_ID,
            slug=unique_slug("autumn-build-weekend"),
            name="Autumn Build Weekend",
            tagline="A practice round that is open right now, so you can try teams, drafts and the deadline yourself.",
            description=DEMO_DESCRIPTION,
            location="Online",
            timezone="Asia/Kolkata",
            starts_at=now - timedelta(days=1),
            submissions_close_at=close,
            judging_ends_at=close + timedelta(days=4),
            results_at=close + timedelta(days=5),
            max_team_size=4,
            is_published=True,
            created_by=organizer,
        )
        EventRole.objects.create(event=event, user=organizer, role=EventRole.Role.ORGANIZER)
        tracks = [
            Track.objects.create(event=event, position=i, name=name, description=desc)
            for i, (name, desc) in enumerate([
                ("Tools for makers", "Things that help people build other things."),
                ("Civic tech", "Services, data and tools for the public good."),
                ("Play", "Games, toys and delightful nonsense."),
            ], start=1)
        ]
        prizes = [
            ("Best overall", "$1,500", None, "Judged across every track."),
            ("Runner-up", "$500", None, ""),
        ] + [("Track winner", "Mechanical keyboard", t, "") for t in tracks] + [
            ("Best first-timer team", "Conference tickets", None, "Every member's first hackathon."),
        ]
        for position, (name, value, track, desc) in enumerate(prizes, start=1):
            Prize.objects.create(event=event, name=name, value=value, track=track, description=desc,
                                 position=position)
        questions = [
            ("Which third-party APIs, datasets or templates did you use?", CustomQuestion.Kind.LONG,
             "Write “none” if you used none.", True, True),
            ("Is this your team's first hackathon?", CustomQuestion.Kind.YESNO, "", False, True),
            ("What would you build next with another weekend?", CustomQuestion.Kind.LONG, "", False, True),
            ("Which country should prizes ship to if you win?", CustomQuestion.Kind.SHORT,
             "Only the organizers see this.", False, False),
        ]
        for position, (prompt, kind, help_text, required, public) in enumerate(questions, start=1):
            CustomQuestion.objects.create(event=event, prompt=prompt, kind=kind, help_text=help_text,
                                          required=required, is_public=public, position=position)

    def seed_voting(self, event, access, closes_at):
        """Quadratic, 25 credits, 3 votes at most on one project: the tested
        defaults (JUDGING.md, section 10). Created once, never overwritten."""
        VotingConfig.objects.get_or_create(event=event, defaults={
            "is_enabled": True, "access": access, "closes_at": closes_at,
        })

    def seed_webhook(self, event, organizer):
        """One webhook, pointed at the portal's own receiver, so the demo's
        delivery log fills up as people click around, with no network."""
        from webhooks.models import Webhook
        from webhooks.services import self_test_url

        if Webhook.objects.filter(event=event, description=DEMO_WEBHOOK).exists():
            return
        hook = Webhook.objects.create(event=event, url="http://127.0.0.1/", description=DEMO_WEBHOOK,
                                      created_by=organizer)
        hook.url = self_test_url(hook)
        hook.save(update_fields=["url"])

    def seed_records(self, event, organizer):
        """Certificates and judges' records for the fixture event, as its
        organizer would issue them once the deadline has passed. The fixture
        has no prizes, so no winners' certificates: add a prize and give it to
        try those. Issuing is idempotent, so later boots add nothing."""
        from records.services import issue

        issue(organizer, event, ["participant", "judge", "organizer"])

    def seed_demo_sessions(self):
        """Write the checker's session cookies straight into the session table."""
        expires = timezone.now() + timedelta(days=30)
        store = SessionStore()
        for entry in DEMO_LOGINS:
            if not entry["cookie"]:
                continue
            user = User.objects.filter(**entry["lookup"]).first()
            if user is None:
                self.warnings.append(f"demo login {entry['key']} has no account")
                continue
            data = {
                SESSION_KEY: str(user.pk),
                BACKEND_SESSION_KEY: "django.contrib.auth.backends.ModelBackend",
                HASH_SESSION_KEY: user.get_session_auth_hash(),
            }
            Session.objects.update_or_create(
                session_key=entry["cookie"],
                defaults={"session_data": store.encode(data), "expire_date": expires},
            )
            UserSession.objects.update_or_create(
                session_key=entry["cookie"],
                defaults={"user": user, "label": "Seeded demo session (checker cookie)"},
            )

    def remove_demo_sessions(self):
        keys = [entry["cookie"] for entry in DEMO_LOGINS if entry["cookie"]]
        Session.objects.filter(session_key__in=keys).delete()
        UserSession.objects.filter(session_key__in=keys).delete()

    # ------------------------------------------------------------------ run

    def run(self, fixture=None):
        with transaction.atomic():
            event = self.seed_fixture(fixture) if fixture else None
            if event is not None:
                self.seed_judging(event, fixture)
            if self.demo:
                organizer = self.seed_people(event)
                self.seed_demo_event(organizer)
                demo_event = Event.objects.filter(external_id=DEMO_EVENT_ID).first()
                if demo_event is not None:
                    self.seed_voting(demo_event, VotingConfig.Access.ACCOUNT,
                                     demo_event.submissions_close_at + timedelta(days=2))
                if event is not None:
                    self.seed_topup(event, organizer)
                    closes = timezone.now().replace(minute=0, second=0, microsecond=0) + timedelta(days=14)
                    self.seed_voting(event, VotingConfig.Access.EMAIL, closes)
                    self.seed_webhook(event, organizer)
                    self.seed_records(event, organizer)
                self.seed_demo_sessions()
            else:
                self.remove_demo_sessions()
        return event


DEMO_DESCRIPTION = """\
This event exists so you can try the whole flow without waiting for a real hackathon.

**How it works**

1. Start a team from the panel on the right, or open an invite link a teammate sent you.
2. Save a draft of your project. Drafts are private to your team and the organizers.
3. Submit before the deadline. You can keep editing until the deadline passes.
4. When the clock runs out the form locks. That includes the API.

**House rules**

- Teams of up to four.
- All project code written during the event window. Libraries and AI tools are fine.
- One submission per team.

Questions? Ask an organizer on the event channel.
"""
