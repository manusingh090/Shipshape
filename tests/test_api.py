"""The REST API, through real requests with real tokens."""

import json
from datetime import timedelta
from unittest import mock

from django.test import Client, TestCase
from django.utils import timezone

from accounts.models import User
from api.models import ApiToken
from events.models import Activity, Event, EventRole
from integrity import limits
from judging.models import Assignment, Criterion, Score
from projects.models import Comment, Project
from voting.models import Ballot, VotingConfig

from .factories import PASSWORD, add_question, add_staff, complete_submission, make_event, make_team, make_user, png_upload

V1 = "/api/v1/"


class ApiClient:
    """Sends JSON with a bearer token, like a script would."""

    def __init__(self, user=None, token=None):
        self.client = Client()
        self.token = token or (ApiToken.issue(user, "test")[1] if user else None)

    def _headers(self):
        return {"HTTP_AUTHORIZATION": f"Bearer {self.token}"} if self.token else {}

    def call(self, method, path, body=None, **extra):
        kwargs = {**self._headers(), **extra}
        if body is not None:
            kwargs.update(data=json.dumps(body), content_type="application/json")
        return getattr(self.client, method.lower())(V1 + path, **kwargs)

    def get(self, path, **params):
        return self.client.get(V1 + path, params, **self._headers())

    def post(self, path, body=None):
        return self.call("POST", path, body if body is not None else {})

    def patch(self, path, body):
        return self.call("PATCH", path, body)

    def put(self, path, body):
        return self.call("PUT", path, body)

    def delete(self, path, body=None):
        return self.call("DELETE", path, body)

    def upload(self, path, files):
        return self.client.post(V1 + path, files, **self._headers())


class AuthTests(TestCase):
    def setUp(self):
        self.user = make_user("ada@example.org")

    def test_a_token_from_a_password_works_and_is_shown_once(self):
        response = Client().post(V1 + "auth/tokens", json.dumps({"email": "ada@example.org", "password": PASSWORD,
                                                                 "name": "script"}), content_type="application/json")
        self.assertEqual(response.status_code, 200)
        raw = response.json()["token"]
        self.assertTrue(raw.startswith("ss_"))
        self.assertNotEqual(ApiToken.objects.get().token_hash, raw)  # only the hash is kept
        me = ApiClient(token=raw).get("me")
        self.assertEqual(me.json()["email"], "ada@example.org")
        listed = ApiClient(token=raw).get("auth/tokens").json()["results"][0]
        self.assertNotIn("token", listed)

    def test_wrong_passwords_are_refused_and_limited_like_sign_in(self):
        for _ in range(5):
            r = Client().post(V1 + "auth/tokens", json.dumps({"email": "ada@example.org", "password": "nope nope"}),
                              content_type="application/json")
            self.assertEqual(r.status_code, 401)
        r = Client().post(V1 + "auth/tokens", json.dumps({"email": "ada@example.org", "password": PASSWORD}),
                          content_type="application/json")
        self.assertEqual(r.status_code, 429)
        self.assertTrue(Activity.objects.filter(event__isnull=True, verb="rate.limited").exists())

    def test_revoked_tokens_deactivated_owners_and_password_changes_all_stop_a_token(self):
        api = ApiClient(self.user)
        self.assertEqual(api.get("me").status_code, 200)
        token = ApiToken.objects.get()
        self.assertEqual(api.delete(f"auth/tokens/{token.pk}").status_code, 200)
        self.assertEqual(api.get("me").status_code, 401)

        api = ApiClient(self.user)
        User.objects.filter(pk=self.user.pk).update(is_active=False)
        self.assertEqual(api.get("me").status_code, 401)
        User.objects.filter(pk=self.user.pk).update(is_active=True)

        api = ApiClient(self.user)
        r = api.post("me/password", {"old_password": PASSWORD, "new_password": "a brand new passphrase"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(api.get("me").status_code, 401)  # the password change revoked it

    def test_nonsense_authorization_is_refused_not_ignored(self):
        self.assertEqual(Client().get(V1 + "me", HTTP_AUTHORIZATION="Bearer ss_madeup").status_code, 401)
        self.assertEqual(Client().get(V1 + "me", HTTP_AUTHORIZATION="Basic abc").status_code, 401)
        self.assertEqual(Client().get(V1 + "me").status_code, 401)

    def test_browser_sessions_get_the_cross_site_defence(self):
        browser = Client()
        browser.force_login(self.user)
        self.assertEqual(browser.get(V1 + "me").status_code, 200)
        # A form on another site can POST, but not as JSON.
        self.assertEqual(browser.post(V1 + "me/sessions/end-others").status_code, 415)
        r = browser.post(V1 + "me/sessions/end-others", "{}", content_type="application/json",
                         HTTP_ORIGIN="https://evil.example")
        self.assertEqual(r.status_code, 403)
        self.assertEqual(browser.post(V1 + "me/sessions/end-others", "{}", content_type="application/json").status_code, 200)

    def test_signup_makes_an_account_and_a_token(self):
        r = Client().post(V1 + "auth/signup", json.dumps({"name": "Grace", "email": "grace@example.org",
                                                         "password": "long enough passphrase"}),
                          content_type="application/json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(ApiClient(token=r.json()["token"]).get("me").json()["email"], "grace@example.org")
        bad = Client().post(V1 + "auth/signup", json.dumps({"name": "X", "email": "x@example.org", "password": "short"}),
                            content_type="application/json")
        self.assertEqual(bad.status_code, 400)
        self.assertIn("password1", bad.json()["fields"])

    def test_writes_are_rate_limited(self):
        api = ApiClient(self.user)
        old = limits.LIMITS["api.write"]
        with mock.patch.dict(limits.LIMITS, {"api.write": limits.Limit(old.scope, old.what, 2, old.window, old.per, old.why)}):
            codes = [api.patch("me", {"name": f"Ada {n}"}).status_code for n in range(3)]
        self.assertEqual(codes, [200, 200, 429])

    def test_the_index_and_reference_are_built_from_the_router(self):
        index = Client().get(V1).json()
        from api.core import ENDPOINTS
        self.assertEqual(len(index["endpoints"]), len(ENDPOINTS))
        page = Client().get(V1 + "docs")
        self.assertContains(page, "/api/v1/events/{slug}/webhooks")
        self.assertContains(page, "Same as: Console, Webhooks")


class ConsoleTests(TestCase):
    def setUp(self):
        self.organizer = make_user("org@example.org", role=User.Role.ORGANIZER)
        self.api = ApiClient(self.organizer)
        r = self.api.post("events", {"name": "Script Hack", "timezone": "Asia/Kolkata",
                                     "starts_at": "2026-12-01T09:00", "submissions_close_at": "2026-12-03T09:00",
                                     "tracks": ["Tools", "Play"]})
        self.assertEqual(r.status_code, 200, r.content)
        self.slug = r.json()["slug"]
        self.event = Event.objects.get(slug=self.slug)

    def test_create_then_edit_an_event_with_iso_dates(self):
        self.assertEqual([t["name"] for t in self.api.get(f"events/{self.slug}/tracks").json()["results"]],
                         ["Tools", "Play"])
        r = self.api.patch(f"events/{self.slug}", {"submissions_close_at": "2026-12-03T12:30Z", "is_published": True})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["submissions_close_at"], "2026-12-03T12:30:00Z")
        self.assertTrue(r.json()["is_published"])
        self.assertTrue(Activity.objects.filter(event=self.event, verb="event.dates").exists())
        bad = self.api.patch(f"events/{self.slug}", {"submissions_close_at": "2026-11-01T00:00Z"})
        self.assertEqual(bad.status_code, 400)
        self.assertIn("submissions_close_at", bad.json()["fields"])

    def test_tracks_prizes_questions_and_organizers(self):
        track = self.api.post(f"events/{self.slug}/tracks", {"name": "Climate"}).json()
        self.assertEqual(self.api.patch(f"events/{self.slug}/tracks/{track['id']}", {"description": "Warm"}).json()["description"], "Warm")
        prize = self.api.post(f"events/{self.slug}/prizes", {"name": "Best overall", "value": "$500"})
        self.assertEqual(prize.status_code, 200, prize.content)
        q = self.api.post(f"events/{self.slug}/questions", {"prompt": "Pick a colour", "kind": "choice",
                                                            "options": ["Red", "Blue"]}).json()
        self.assertEqual(q["options"], ["Red", "Blue"])
        self.assertEqual(self.api.delete(f"events/{self.slug}/tracks/{track['id']}").status_code, 200)
        helper = make_user("helper@example.org")
        role = self.api.post(f"events/{self.slug}/organizers", {"email": "helper@example.org"}).json()
        self.assertEqual(ApiClient(helper).get(f"events/{self.slug}/submissions").status_code, 200)
        me = [r for r in self.api.get(f"events/{self.slug}/organizers").json()["results"] if r["user"]["id"] == self.organizer.pk][0]
        self.assertEqual(self.api.delete(f"events/{self.slug}/organizers/{me['id']}").status_code, 200)
        last = ApiClient(helper).delete(f"events/{self.slug}/organizers/{role['id']}")
        self.assertEqual(last.status_code, 400)
        self.assertIn("at least one organizer", last.json()["detail"])

    def test_outsiders_are_refused_the_console(self):
        stranger = ApiClient(make_user("x@example.org"))
        self.assertEqual(stranger.get(f"events/{self.slug}/submissions").status_code, 404)  # unpublished: invisible
        self.api.patch(f"events/{self.slug}", {"is_published": True})
        for path in ("submissions", "teams", "activity", "judging/judges", "voting", "integrity", "webhooks"):
            self.assertEqual(stranger.get(f"events/{self.slug}/{path}").status_code, 403, path)
        self.assertEqual(stranger.patch(f"events/{self.slug}", {"name": "Mine now"}).status_code, 403)
        self.assertEqual(ApiClient(make_user("m@example.org")).post("events", {"name": "Nope"}).status_code, 403)


class ChangeLogTests(TestCase):
    """What changed must be logged from the page and the API alike. (A ModelForm
    copies new values onto the object while validating, so a service that reads
    "before" from the object logs nothing; these would catch that.)"""

    def setUp(self):
        self.event = make_event(slug="logged", phase="open")
        self.organizer = make_user("org@example.org", role=User.Role.ORGANIZER)
        add_staff(self.event, self.organizer)

    def test_a_moved_deadline_is_logged_from_the_console_page(self):
        browser = Client()
        browser.force_login(self.organizer)
        from django.urls import reverse
        from events.forms import EventForm
        from api.endpoints.events import current

        data = current(EventForm(instance=self.event))
        data["submissions_close_at"] = (self.event.submissions_close_at + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M")
        data["starts_at"] = data["starts_at"].strftime("%Y-%m-%dT%H:%M")
        data = {k: v for k, v in data.items() if v is not False}
        response = browser.post(reverse("events:manage_details", args=[self.event.slug]), data)
        self.assertEqual(response.status_code, 302, [str(e) for e in response.context["form"].errors.items()] if response.context else "")
        details = list(Activity.objects.filter(event=self.event, verb="event.dates").values_list("detail", flat=True))
        self.assertTrue(any("moved the submission deadline" in d for d in details), details)

    def test_a_weight_change_is_logged_as_one(self):
        c = Criterion.objects.create(event=self.event, key="impact", label="Impact", weight=1)
        ApiClient(self.organizer).patch(f"events/logged/judging/criteria/{c.pk}", {"weight": 2})
        self.assertIn("changed the weight of “Impact” from 1.00 to 2", Activity.objects.get(verb="judging.rubric").detail)


class ParticipantTests(TestCase):
    def setUp(self):
        self.event = make_event(slug="open-hack", phase="open")
        self.captain = make_user("cap@example.org")
        self.api = ApiClient(self.captain)

    def test_a_team_from_start_to_finish(self):
        team = self.api.post("events/open-hack/team", {"name": "Owls"}).json()
        code = team["invite_code"]
        friend = ApiClient(make_user("friend@example.org"))
        self.assertEqual(friend.get(f"invites/{code}").json()["name"], "Owls")
        self.assertEqual(friend.post(f"invites/{code}/join").status_code, 200)
        self.assertEqual(self.api.patch("events/open-hack/team", {"name": "Night Owls"}).json()["name"], "Night Owls")
        new = self.api.post("events/open-hack/team/invite/reset").json()["invite_code"]
        self.assertNotEqual(new, code)
        friend_id = User.objects.get(email="friend@example.org").pk
        self.assertEqual(friend.delete(f"events/open-hack/team/members/{self.captain.pk}").status_code, 400)  # not captain
        self.assertEqual(self.api.delete(f"events/open-hack/team/members/{friend_id}").status_code, 200)
        self.assertTrue(self.api.post("events/open-hack/team/leave").json()["team_dissolved"])

    def test_submit_upload_withdraw_and_the_deadline(self):
        self.api.post("events/open-hack/team", {"name": "Owls"})
        q = add_question(self.event)
        body = {"action": "submit", **complete_submission(self.event), "answers": {str(q.pk): "None"}}
        r = self.api.post("events/open-hack/submission", body)
        self.assertEqual(r.status_code, 201, r.content)
        up = self.api.upload("events/open-hack/submission/images", {"images": [png_upload(), png_upload("b.png")]})
        self.assertEqual(up.json()["added"], 2)
        thumb = self.api.upload("events/open-hack/submission/thumbnail", {"thumbnail": png_upload("t.png")})
        self.assertIsNotNone(thumb.json()["thumbnail"])
        image_id = Project.objects.get().images.first().pk
        self.assertEqual(self.api.delete(f"events/open-hack/submission/images/{image_id}").status_code, 200)
        self.assertEqual(self.api.post("events/open-hack/submission/withdraw").json()["status"], "draft")

        browser = Client()
        browser.force_login(self.captain)
        self.assertEqual(browser.post(V1 + "events/open-hack/submission/images", {"images": [png_upload()]}).status_code, 401)

        Event.objects.filter(pk=self.event.pk).update(submissions_close_at=timezone.now() - timedelta(minutes=1),
                                                       starts_at=timezone.now() - timedelta(days=1))
        late = self.api.post("events/open-hack/submission", {"action": "save", "title": "Too late"})
        self.assertEqual((late.status_code, late.json()["error"]), (403, "submissions_closed"))
        self.assertEqual(self.api.post("events/open-hack/submission/withdraw").status_code, 403)

    def test_staff_cant_compete_through_the_api_either(self):
        judge = make_user("judge@example.org")
        EventRole.objects.create(event=self.event, user=judge, role=EventRole.Role.JUDGE)
        r = ApiClient(judge).post("events/open-hack/team", {"name": "Sneaky"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("staff", r.json()["detail"])

    def test_gallery_project_and_comments(self):
        closed = make_event(slug="done-hack", phase="closed")
        team = make_team(closed, make_user("t@example.org"))
        private = add_question(closed, prompt="Shipping address?", public=False)
        p = Project.objects.create(event=closed, team=team, track=closed.tracks.get(), title="Quiet Hours",
                                   tagline="x", status="submitted", submitted_at=closed.submissions_close_at)
        p.answers.create(question=private, value="42 Secret Street")
        self.assertEqual(ApiClient().get("projects", q="quiet").json()["count"], 1)
        self.assertEqual(ApiClient().get("projects", q="nothing like it").json()["count"], 0)
        self.assertNotIn("Secret Street", ApiClient().get(f"projects/{p.pk}").content.decode())
        c = self.api.post(f"projects/{p.pk}/comments", {"body": "Nice **work**"}).json()
        self.assertEqual(ApiClient().get(f"projects/{p.pk}/comments").json()["results"][0]["body"], "Nice **work**")
        self.assertEqual(ApiClient(make_user("o@example.org")).delete(f"projects/{p.pk}/comments/{c['id']}").status_code, 403)
        self.assertEqual(self.api.delete(f"projects/{p.pk}/comments/{c['id']}").status_code, 200)
        self.assertTrue(Comment.objects.get().removed_at)


class JudgingApiTests(TestCase):
    def setUp(self):
        self.event = make_event(slug="judged", phase="closed")
        self.track = self.event.tracks.get()
        self.organizer = make_user("org@example.org", role=User.Role.ORGANIZER)
        add_staff(self.event, self.organizer)
        self.org = ApiClient(self.organizer)
        self.projects = []
        for n in range(3):
            team = make_team(self.event, make_user(f"t{n}@example.org"), name=f"T{n}")
            self.projects.append(Project.objects.create(event=self.event, team=team, track=self.track, title=f"P{n}",
                                                        tagline="x", status="submitted",
                                                        submitted_at=self.event.submissions_close_at))
        self.ada, self.bo = make_user("ada@example.org"), make_user("bo@example.org")

    def test_the_organizer_side(self):
        for key in ("impact", "craft"):
            self.assertEqual(self.org.post("events/judged/judging/criteria", {"label": key.title()}).status_code, 200)
        self.assertEqual(self.org.patch("events/judged/judging/config", {"reviews_per_project": 2}).json()["reviews_per_project"], 2)
        for email in ("ada@example.org", "bo@example.org"):
            r = self.org.post("events/judged/judging/judges", {"email": email, "tracks": [self.track.pk]})
            self.assertEqual(r.status_code, 200, r.content)
        preview = self.org.post("events/judged/judging/batches", {"label": "B1", "target": 2, "preview": True}).json()
        self.assertFalse(preview["saved"])
        self.assertEqual(Assignment.objects.count(), 0)
        commit = self.org.post("events/judged/judging/batches", {"label": "B1", "target": 2, "seed": preview["seed"]}).json()
        self.assertEqual(commit["plan"], preview["plan"])  # the same seed commits the same plan
        self.assertEqual(Assignment.objects.count(), 6)
        invite = self.org.post("events/judged/judging/invites", {"tracks": [self.track.pk]}).json()
        newcomer = ApiClient(make_user("new@example.org"))
        self.assertEqual(newcomer.post(f"judge-invites/{invite['token']}/accept").status_code, 200)
        self.assertEqual(len(self.org.get("events/judged/judging/judges").json()["results"]), 3)
        self.assertEqual(self.org.get("events/judged/export/final-rankings.csv").status_code, 200)

    def test_the_judges_side_and_isolation(self):
        Criterion.objects.create(event=self.event, key="impact", label="Impact", weight=1)
        for judge in (self.ada, self.bo):
            role = EventRole.objects.create(event=self.event, user=judge, role=EventRole.Role.JUDGE)
            role.tracks.set([self.track])
        Assignment.objects.create(event=self.event, judge=self.ada, project=self.projects[0])
        Assignment.objects.create(event=self.event, judge=self.bo, project=self.projects[1])
        ada, bo = ApiClient(self.ada), ApiClient(self.bo)
        self.assertEqual([r["project"] for r in ada.get("judge/events/judged/queue").json()["results"]], [self.projects[0].pk])
        r = ada.put(f"judge/events/judged/projects/{self.projects[0].pk}/score",
                    {"criteria": {"impact": 4}, "comment": "ADA SECRET", "submit": True})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertTrue(r.json()["score"]["submitted"])
        # Bo can't open Ada's project, score it, or read her scores.
        self.assertEqual(bo.get(f"judge/events/judged/projects/{self.projects[0].pk}").status_code, 404)
        self.assertEqual(bo.put(f"judge/events/judged/projects/{self.projects[0].pk}/score",
                                {"criteria": {"impact": 1}, "submit": True}).status_code, 404)
        self.assertEqual(bo.get("judge/scores", judge=str(self.ada.pk)).status_code, 403)
        self.assertNotIn("ADA SECRET", bo.get("judge/scores").content.decode())
        self.assertEqual(Score.objects.count(), 1)
        self.assertEqual(ApiClient(make_user("p@example.org")).get("judge/scores").status_code, 403)


class VotingApiTests(TestCase):
    def setUp(self):
        self.event = make_event(slug="voted", phase="closed")
        self.organizer = make_user("org@example.org", role=User.Role.ORGANIZER)
        add_staff(self.event, self.organizer)
        team = make_team(self.event, make_user("t@example.org"))
        self.project = Project.objects.create(event=self.event, team=team, track=self.event.tracks.get(), title="P",
                                              tagline="x", status="submitted", submitted_at=self.event.submissions_close_at)
        self.org = ApiClient(self.organizer)

    def test_run_a_vote_end_to_end(self):
        closes = (timezone.now() + timedelta(days=1)).strftime("%Y-%m-%dT%H:%MZ")
        r = self.org.patch("events/voted/voting", {"is_enabled": True, "access": "account", "closes_at": closes})
        self.assertEqual(r.status_code, 200, r.content)
        voter = ApiClient(make_user("v@example.org"))
        self.assertEqual(voter.post("events/voted/ballot", {"votes": {str(self.project.pk): 3}}).status_code, 200)
        hidden = ApiClient().get("events/voted/vote/results")
        self.assertEqual((hidden.status_code, hidden.json()["error"]), (403, "results_hidden"))
        self.assertEqual(self.org.get("events/voted/voting/tally").json()["voted"], 1)
        self.assertEqual(self.org.post("events/voted/voting/publish").status_code, 400)  # still open
        VotingConfig.objects.filter(event=self.event).update(closes_at=timezone.now() - timedelta(minutes=1))
        self.assertEqual(self.org.post("events/voted/voting/publish").status_code, 200)
        self.assertEqual(ApiClient().get("events/voted/vote/results").json()["results"][0]["votes"], 3)
        ballot = Ballot.objects.get()
        self.assertEqual(self.org.post("events/voted/integrity/ballots/exclude",
                                       {"ballots": [ballot.pk], "reason": "test"}).json()["excluded"], 1)
        self.assertEqual(ApiClient().get("events/voted/vote/results").json()["ballots"], 0)


class AdminApiTests(TestCase):
    def test_only_admins_change_roles(self):
        admin = make_user("admin@example.org", role=User.Role.ADMIN)
        member = make_user("m@example.org")
        self.assertEqual(ApiClient(member).get("admin/users").status_code, 403)
        r = ApiClient(admin).patch(f"admin/users/{member.pk}", {"platform_role": "organizer"})
        self.assertEqual(r.json()["platform_role"], "organizer")
        self.assertIn("made M", ApiClient(admin).get("admin/audit").json()["results"][0]["detail"])
        self.assertEqual(ApiClient(admin).patch(f"admin/users/{admin.pk}", {"platform_role": "member"}).status_code, 400)
