"""The published OpenAPI document, and the API keeping to it.

Two halves. DocumentTests check the document itself: it's valid in shape,
every operation says what it takes and returns, the copy in the repository
is current, and it's served. The journeys below then drive every endpoint
through a realistic event, as an organizer, a judge, a participant, an
admin and an anonymous voter. They make few assertions of their own: during
the test run every /api/v1/ answer is checked against the schema its
operation publishes (api/core._check_contract), so an answer that doesn't
match the document fails the test that received it. The runner fails the
whole run if any endpoint went unexercised.
"""

import json
import re
from datetime import timedelta
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.core import mail
from django.test import Client, TestCase

from accounts.models import User
from api import schema as sc
from api.core import ENDPOINTS
from api.models import ApiToken
from api.openapi import LEGACY, document, openapi_path
from events.models import Event, EventRole
from events.seeding import Seeder, load_fixture
from judging.models import Assignment, Conflict, Score
from projects.models import Project
from records.models import Record
from voting.models import Ballot, VotingConfig

from .factories import add_staff, make_user
from .test_webhooks import public_dns

FIXTURE = Path(settings.BASE_DIR) / "fixtures.json"
COMMITTED = Path(settings.BASE_DIR) / "api" / "openapi.json"
V1 = "/api/v1/"
SLUG = "sample-hack-2026"


class DocumentTests(TestCase):
    def setUp(self):
        self.doc = document()

    def operations(self):
        for path, item in self.doc["paths"].items():
            for method, op in item.items():
                yield path, method, op

    def test_it_is_openapi_3_1_and_names_every_endpoint(self):
        self.assertEqual(self.doc["openapi"], "3.1.0")
        published = {(m.upper(), p) for p, m, _ in self.operations()}
        for e in ENDPOINTS:
            self.assertIn((e.method, openapi_path(e.path)), published, e.path)
        self.assertEqual(len(published), len(ENDPOINTS) + len(LEGACY))

    def test_every_operation_says_what_it_takes_and_returns(self):
        ids = set()
        for path, method, op in self.operations():
            where = f"{method.upper()} {path}"
            self.assertNotIn(op["operationId"], ids, where)
            ids.add(op["operationId"])
            ok = op["responses"]["200"]["content"]
            if "application/json" in ok:
                self.assertTrue(ok["application/json"]["schema"], f"{where} has no response schema")
            declared = {p["name"] for p in op.get("parameters", []) if p.get("in") == "path"}
            self.assertEqual(declared, set(re.findall(r"{(\w+)}", path)), where)
            self.assertIn("401", op["responses"], where)

    def test_every_reference_resolves(self):
        text = json.dumps(self.doc)
        for name in set(re.findall(r'"#/components/(\w+)/(\w+)"', text)):
            self.assertIn(name[1], self.doc["components"][name[0]], name)

    def test_request_bodies_come_from_the_forms_they_are_checked_with(self):
        from events.forms import EventForm

        op = self.doc["paths"]["/v1/events/{slug}"]["patch"]
        props = op["requestBody"]["content"]["application/json"]["schema"]["properties"]
        self.assertEqual(set(props), set(EventForm.Meta.fields))
        self.assertEqual(props["name"]["maxLength"], Event._meta.get_field("name").max_length)

    def test_the_copy_in_the_repository_is_current(self):
        committed = json.loads(COMMITTED.read_text(encoding="utf-8"))
        self.assertEqual(committed, json.loads(json.dumps(self.doc)),
                         "src/api/openapi.json is stale: run python src/manage.py openapi")

    def test_it_is_served(self):
        served = Client().get(V1 + "openapi.json")
        self.assertEqual(served.status_code, 200)
        self.assertEqual(served.json()["info"]["title"], "Shipshape REST API")
        self.assertContains(Client().get(V1 + "docs"), "openapi.json")

    def test_the_validator_refuses_what_the_document_does_not_say(self):
        person = sc.ref("Person")
        self.assertEqual(sc.validate(person, {"id": 1, "name": "Ada"}), [])
        self.assertTrue(sc.validate(person, {"id": 1, "name": "Ada", "email": "a@x.org"}))  # undocumented key
        self.assertTrue(sc.validate(person, {"id": "1", "name": "Ada"}))  # wrong type
        self.assertTrue(sc.validate(person, {"id": 1}))  # missing key
        self.assertTrue(sc.validate(sc.when(), "yesterday"))
        self.assertEqual(sc.validate(sc.nullable(sc.when()), None), [])


class Journey(TestCase):
    """The fixture event (closed, judged), with an organizer and a vote."""

    @classmethod
    def setUpTestData(cls):
        Seeder(demo=False).run(load_fixture(FIXTURE))
        cls.event = Event.objects.get(slug=SLUG)
        cls.organizer = make_user("org@example.org", role=User.Role.ORGANIZER)
        add_staff(cls.event, cls.organizer)
        cls.admin = make_user("admin@example.org", role=User.Role.ADMIN)
        cls.judge = User.objects.get(external_id="jdg_24")
        cls.priya = User.objects.get(email="priya1@example.org")
        cls.listed = list(Project.objects.filter(event=cls.event, status="submitted", duplicate_of__isnull=True)
                          .order_by("pk"))

    def api(self, user):
        return Client(HTTP_AUTHORIZATION=f"Bearer {ApiToken.issue(user, 'test')[1]}")

    def call(self, client, method, path, body=None, status=200):
        kwargs = {}
        if body is not None:
            kwargs = {"data": json.dumps(body), "content_type": "application/json"}
        response = getattr(client, method.lower())(V1 + path, **kwargs)
        self.assertEqual(response.status_code, status, f"{method} {path}: {response.content[:300]}")
        return response.json() if response["Content-Type"].startswith("application/json") else response


class OrganizerJourney(Journey):
    def test_the_console_reads(self):
        org = self.api(self.organizer)
        for path in ("events", f"events/{SLUG}", "organize", f"events/{SLUG}/activity", f"events/{SLUG}/teams",
                     f"events/{SLUG}/prizes", f"events/{SLUG}/questions", f"events/{SLUG}/voting",
                     f"events/{SLUG}/integrity", f"events/{SLUG}/records", f"events/{SLUG}/webhooks",
                     f"events/{SLUG}/judging/config", f"events/{SLUG}/judging/criteria",
                     f"events/{SLUG}/judging/invites", f"events/{SLUG}/judging/conflicts",
                     f"events/{SLUG}/judging/assignments", f"events/{SLUG}/judging/batches",
                     f"events/{SLUG}/judging/progress", f"events/{SLUG}/judging/results", "openapi.json"):
            self.call(org, "GET", path)
        archive = self.call(org, "GET", f"events/{SLUG}/export/archive.zip")
        self.assertEqual(archive["Content-Type"], "application/zip")

    def test_prizes_and_questions_change_and_go(self):
        org = self.api(self.organizer)
        prize = self.call(org, "POST", f"events/{SLUG}/prizes", {"name": "Best tool", "quantity": 1})
        self.call(org, "PATCH", f"events/{SLUG}/prizes/{prize['id']}", {"value": "A trophy"})
        self.call(org, "DELETE", f"events/{SLUG}/prizes/{prize['id']}")
        q = self.call(org, "POST", f"events/{SLUG}/questions", {"prompt": "Which APIs?", "kind": "short"})
        self.call(org, "PATCH", f"events/{SLUG}/questions/{q['id']}", {"required": True})
        self.call(org, "DELETE", f"events/{SLUG}/questions/{q['id']}")

    def test_duplicates_and_comments(self):
        org, a, b = self.api(self.organizer), self.listed[0], self.listed[1]
        self.call(org, "POST", f"events/{SLUG}/integrity/projects/{b.pk}/hide", {"original": a.pk, "reason": "same"})
        self.call(org, "POST", f"events/{SLUG}/submissions/{b.pk}/promote")
        other = self.listed[2]  # a is hidden now: b was listed in its place
        comment = self.call(self.api(self.priya), "POST", f"projects/{other.pk}/comments", {"body": "Buy followers"})
        self.call(org, "POST", f"events/{SLUG}/integrity/comments/remove", {"comments": [comment["id"]], "reason": "spam"})

    def test_a_new_event_gets_a_rubric_and_a_roster(self):
        org = self.api(self.organizer)
        from django.utils import timezone
        now = timezone.now()
        made = self.call(org, "POST", "events", {
            "name": "Spring Build", "starts_at": (now - timedelta(days=1)).isoformat(),
            "submissions_close_at": (now + timedelta(days=2)).isoformat(), "tracks": ["Tools", "Health"]})
        slug = made["slug"]
        c = self.call(org, "POST", f"events/{slug}/judging/criteria", {"label": "Impact", "weight": 2})
        self.call(org, "PATCH", f"events/{slug}/judging/criteria/{c['id']}", {"weight": 3})
        self.call(org, "DELETE", f"events/{slug}/judging/criteria/{c['id']}")
        self.call(org, "POST", f"events/{slug}/import/teams",
                  {"csv": "team,member_email\nOtters,otter@example.net\n", "preview": True})


class JudgingJourney(Journey):
    def free_project(self, judge):
        """A listed project in the judge's tracks that they aren't assigned."""
        role = EventRole.objects.get(event=self.event, user=judge, role="judge")
        tracks = set(role.tracks.values_list("pk", flat=True))
        taken = set(Assignment.objects.filter(event=self.event, judge=judge).values_list("project_id", flat=True))
        taken |= set(Conflict.objects.filter(event=self.event, judge=judge).values_list("project_id", flat=True))
        return next(p for p in self.listed if (role.all_tracks or p.track_id in tracks) and p.pk not in taken)

    def test_assigning_scorecards_conflicts_and_judges(self):
        org, me = self.api(self.organizer), self.api(self.judge)
        mine = EventRole.objects.get(event=self.event, user=self.judge, role="judge")
        self.call(org, "PATCH", f"events/{SLUG}/judging/judges/{mine.pk}", {"all_tracks": True})
        first = self.free_project(self.judge)
        a = self.call(org, "POST", f"events/{SLUG}/judging/assignments", {"judge": self.judge.pk, "project": first.pk})
        self.call(me, "GET", "judge/events")
        self.call(me, "GET", f"judge/events/{SLUG}/projects/{first.pk}")
        self.call(me, "POST", f"judge/events/{SLUG}/projects/{first.pk}/conflict", {"reason": "I mentored them"})
        second = self.free_project(self.judge)
        a = self.call(org, "POST", f"events/{SLUG}/judging/assignments", {"judge": self.judge.pk, "project": second.pk})
        self.call(org, "DELETE", f"events/{SLUG}/judging/assignments/{a['id']}")
        third = self.free_project(self.judge)
        self.call(org, "POST", f"events/{SLUG}/judging/conflicts",
                  {"judge": self.judge.pk, "project": third.pk, "reason": "Their manager"})
        invite = self.call(org, "POST", f"events/{SLUG}/judging/invites", {"all_tracks": True})
        self.call(org, "DELETE", f"events/{SLUG}/judging/invites/{invite['id']}")
        role = EventRole.objects.filter(event=self.event, role="judge").exclude(user=self.judge).first()
        self.call(org, "PATCH", f"events/{SLUG}/judging/judges/{role.pk}", {"all_tracks": True})
        self.call(org, "DELETE", f"events/{SLUG}/judging/judges/{role.pk}")


class RecordsJourney(Journey):
    def test_awards_certificates_receipts_and_revoking(self):
        org = self.api(self.organizer)
        prize = self.call(org, "POST", f"events/{SLUG}/prizes", {"name": "Grand prize", "quantity": 1})
        award = self.call(org, "POST", f"events/{SLUG}/awards", {"prize": prize["id"], "project": self.listed[0].pk})
        self.call(org, "POST", f"events/{SLUG}/records", {"kinds": ["participant", "award", "judge"]})
        self.call(self.api(self.priya), "GET", "me/records")
        judge_record = Record.objects.filter(event=self.event, kind="judge").first()
        self.call(Client(), "GET", f"records/{judge_record.code}")
        self.call(org, "GET", f"records/{judge_record.code}/scores")
        for r in Record.objects.filter(event=self.event, kind="award"):
            self.call(org, "POST", f"records/{r.code}/revoke", {"reason": "Given by mistake"})
        self.call(org, "DELETE", f"events/{SLUG}/awards/{award['id']}")


class VoteJourney(Journey):
    def setUp(self):
        from django.utils import timezone
        self.config = VotingConfig.objects.create(event=self.event, is_enabled=True, access="email",
                                                  closes_at=timezone.now() + timedelta(days=1))

    def test_an_email_voter_from_link_to_ballot(self):
        voter = Client()
        self.call(voter, "POST", f"events/{SLUG}/ballot/email", {"email": "voter@example.net"})
        token = re.search(r"/vote/confirm/([^/\s]+)/", mail.outbox[-1].body).group(1)
        self.call(voter, "POST", f"events/{SLUG}/ballot/email/confirm", {"token": token})
        ballot = self.call(voter, "GET", f"events/{SLUG}/ballot")
        pick = next(p["id"] for p in ballot["projects"] if not p["yours"])
        self.call(voter, "POST", f"events/{SLUG}/ballot", {"votes": {str(pick): 2}})
        self.call(voter, "POST", f"events/{SLUG}/ballot/forget", {})
        org = self.api(self.organizer)
        saved = Ballot.objects.get(event=self.event)
        self.call(org, "POST", f"events/{SLUG}/integrity/ballots/exclude", {"ballots": [saved.pk], "reason": "test"})
        self.call(org, "POST", f"events/{SLUG}/integrity/ballots/{saved.pk}/restore")
        self.call(org, "POST", f"events/{SLUG}/voting/link")

    def test_published_results_come_down(self):
        from django.utils import timezone
        VotingConfig.objects.filter(pk=self.config.pk).update(closes_at=timezone.now() - timedelta(minutes=1),
                                                              results_published_at=timezone.now())
        self.call(self.api(self.organizer), "POST", f"events/{SLUG}/voting/unpublish")


@mock.patch("webhooks.delivery.socket.getaddrinfo", public_dns)
class AdminJourney(Journey):
    def test_people_and_platform_webhooks(self):
        admin = self.api(self.admin)
        self.call(admin, "GET", "admin/users")
        hook = self.call(admin, "POST", "admin/webhooks", {"url": "https://hooks.example.org/in"})
        self.call(admin, "GET", "admin/webhooks")
        self.call(admin, "GET", f"webhooks/{hook['id']}")

    def test_signed_in_browsers(self):
        here, there = Client(), Client()
        here.force_login(self.priya)
        there.force_login(self.priya)
        there.get("/")
        sessions = self.call(here, "GET", "me/sessions")["results"]
        other = next(s for s in sessions if not s["current"])
        self.call(here, "DELETE", f"me/sessions/{other['id']}")


class ParticipantJourney(Journey):
    def test_team_and_submission_after_the_deadline(self):
        me = self.api(self.priya)
        self.call(me, "GET", f"events/{SLUG}/team")
        self.call(me, "GET", f"events/{SLUG}/submission")
        late = self.call(me, "POST", f"events/{SLUG}/submission", {"title": "late"}, status=403)
        self.assertEqual(late["error"], "submissions_closed")


class OlderRouteTests(Journey):
    """The older /api/ routes aren't served through api/core, so they're
    checked here against what the document says about them."""

    def check(self, client, method, path, status=200, body=None):
        op = document()["paths"][path[len("/api"):].replace(SLUG, "{slug}")][method.lower()]
        kwargs = {"data": json.dumps(body), "content_type": "application/json"} if body is not None else {}
        response = getattr(client, method.lower())(path, **kwargs)
        self.assertEqual(response.status_code, status, f"{method} {path}: {response.content[:200]}")
        if response["Content-Type"].startswith("application/json"):
            schema = (op["responses"]["200"]["content"]["application/json"]["schema"] if status == 200
                      else sc.ref("Error"))
            self.assertEqual(sc.validate(schema, response.json()), [], f"{method} {path}")
        return response

    def test_each_older_route_matches_the_document(self):
        judge, priya, org = Client(), Client(), Client()
        judge.force_login(self.judge)
        priya.force_login(self.priya)
        org.force_login(self.organizer)
        self.check(Client(), "GET", "/api/projects")
        self.check(priya, "GET", f"/api/events/{SLUG}/submission")
        self.check(priya, "POST", f"/api/events/{SLUG}/submission", status=403, body={"title": "late"})
        self.check(judge, "GET", "/api/judge/scores")
        self.check(judge, "GET", "/api/judge/assignments")
        scored = Score.objects.filter(judge=self.judge, submitted_at__isnull=False).select_related("project").first()
        marks = {i.criterion.key: i.value for i in scored.items.select_related("criterion")}
        self.check(judge, "POST", f"/api/events/{SLUG}/judging/scores",
                   body={"project": scored.project_id, "criteria": marks, "submit": True})
        self.check(org, "GET", f"/api/events/{SLUG}/judging/progress")
        self.check(org, "GET", f"/api/events/{SLUG}/judging/results")
        csv = org.get(f"/api/events/{SLUG}/export/final-rankings.csv")
        self.assertEqual(csv["Content-Type"].split(";")[0], "text/csv")
        from django.utils import timezone
        VotingConfig.objects.create(event=self.event, is_enabled=True, access="account",
                                    closes_at=timezone.now() + timedelta(days=1))
        self.check(priya, "GET", f"/api/events/{SLUG}/ballot")
        own = set(Project.objects.filter(team__memberships__user=self.priya).values_list("pk", flat=True))
        pick = next(p.pk for p in self.listed if p.pk not in own)
        self.check(priya, "POST", f"/api/events/{SLUG}/ballot", body={"votes": {str(pick): 1}})
        self.check(priya, "GET", f"/api/events/{SLUG}/vote/results", status=403)
        self.check(org, "GET", f"/api/events/{SLUG}/vote/results")
