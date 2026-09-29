"""The threat model (JUDGING.md, section 11), one test per claim.

Each test is named after the attack's id in that section. "Stopped" tests
try the attack and expect a refusal. "Gap" tests pin down an attack the
portal does not stop, so that if the behaviour ever changes, this file fails
and the threat model has to be rewritten to match. Claims already covered
elsewhere are cited in section 11 by their test name instead.
"""

import json
from datetime import timedelta
from unittest import mock

from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from events import services as event_services
from events.deadline import WindowError
from events.models import Event, EventRole
from integrity import detect
from judging.models import Assignment, AssignmentBatch, Criterion
from judging.scoring import save_score
from projects.models import Project, ProjectImage
from projects.services import SubmissionError, add_images, withdraw_submission
from teams.services import TeamError, join_team
from voting import services as vote
from voting.models import Ballot, VotingConfig

from .factories import add_staff, make_event, make_team, make_user, png_upload
from .test_integrity import tighter

V1 = "/api/v1/"


class VoteSetup(TestCase):
    access = VotingConfig.Access.EMAIL

    def setUp(self):
        self.event = make_event(slug="vote-attacks", phase="closed")
        self.track = self.event.tracks.get()
        self.organizer = make_user("org@example.org", role=User.Role.ORGANIZER)
        add_staff(self.event, self.organizer)
        self.captain = make_user("ada.lovelace@gmail.com")
        self.own = self.listed("Ada's project", self.captain)
        self.other = self.listed("Someone else's", make_user("sam@example.org"))
        self.config = VotingConfig.objects.create(event=self.event, is_enabled=True, access=self.access,
                                                  closes_at=timezone.now() + timedelta(days=1))

    def listed(self, title, captain):
        team = make_team(self.event, captain, name=f"{title} team")
        return Project.objects.create(event=self.event, team=team, track=self.track, title=title, tagline="x",
                                      status=Project.Status.SUBMITTED,
                                      submitted_at=self.event.submissions_close_at - timedelta(hours=1))

    def email_ballot(self, email, votes=None, ip="net-a"):
        voter = vote.Voter(kind=Ballot.Kind.EMAIL, email=email, ip_hash=ip)
        return vote.save_ballot(self.event, voter, votes or {self.other.pk: 1})[0]


class SybilTests(VoteSetup):
    def test_SY2_one_inbox_is_one_ballot_however_the_address_is_spelled(self):
        first = self.email_ballot("mallory@gmail.com")
        for alias in ("Mal.Lory@gmail.com", "mallory+2@gmail.com", "m.a.l.l.o.r.y+vote@googlemail.com"):
            self.assertEqual(self.email_ballot(alias).pk, first.pk, alias)
        self.assertEqual(Ballot.objects.filter(event=self.event).count(), 1)
        other_provider = self.email_ballot("mallory+2@example.org")
        self.assertEqual(self.email_ballot("mallory@example.org").pk, other_provider.pk)

    def test_SY2_asking_for_links_counts_the_inbox_not_the_spelling(self):
        for n in range(3):
            vote.request_email_pass(self.event, f"mallory+{n}@example.org", "net-a", lambda t: f"/c/{t}")
        with self.assertRaises(vote.RateLimited):
            vote.request_email_pass(self.event, "mallory+9@example.org", "net-b", lambda t: "/c/x")

    def test_SY7_a_teammate_cannot_back_their_own_team_through_an_alias(self):
        with self.assertRaises(vote.NotEligible):
            self.email_ballot("adalovelace+fan@googlemail.com", {self.own.pk: 1})

    def test_SY3_gap_separate_real_inboxes_are_separate_voters(self):
        a = self.email_ballot("one@example.org")
        b = self.email_ballot("two@example.org")
        self.assertNotEqual(a.pk, b.pk)  # nothing inside the portal can tell two real people from one person

    def test_SY6_a_forged_forwarded_for_header_does_not_change_the_network(self):
        client = Client()
        seen = []
        with mock.patch.object(vote.rate_limits, "network_key", side_effect=lambda ip: seen.append(ip) or "k"):
            for fake in ("1.1.1.1", "8.8.8.8"):
                client.post(reverse("voting:ballot", args=[self.event.slug]),
                            {"op": "email", "email": f"x{fake}@example.org"}, HTTP_X_FORWARDED_FOR=fake)
        self.assertTrue(seen)
        self.assertEqual(set(seen), {"127.0.0.1"})


class LinkSybilTests(VoteSetup):
    access = VotingConfig.Access.LINK

    def test_SY5_gap_a_fresh_browser_gets_a_fresh_ballot_on_an_open_link(self):
        url = reverse("voting:ballot_link", args=[self.event.slug, self.config.link_token])
        for _ in range(2):
            browser = Client()  # a private window, or cookies cleared
            browser.get(url)
            browser.post(reverse("voting:api_ballot", args=[self.event.slug]) + f"?link={self.config.link_token}",
                         data=json.dumps({"votes": {str(self.other.pk): 1}}), content_type="application/json")
        self.assertEqual(Ballot.objects.filter(event=self.event).count(), 2)

    def test_BS3_a_script_opening_ballots_from_one_network_is_cut_off(self):
        with tighter("vote.new_ballot", 3):
            for n in range(3):
                vote.save_ballot(self.event, vote.Voter(kind=Ballot.Kind.LINK, ballot_key="", order_token=f"o{n}",
                                                        ip_hash="net-bot"), {self.other.pk: 1})
            with self.assertRaises(vote.RateLimited):
                vote.save_ballot(self.event, vote.Voter(kind=Ballot.Kind.LINK, order_token="o9", ip_hash="net-bot"),
                                 {self.other.pk: 1})


class AccountSybilTests(VoteSetup):
    access = VotingConfig.Access.ACCOUNT

    def test_SY4_gap_throwaway_accounts_vote_but_are_flagged_as_fresh(self):
        sock = make_user("sock1@example.org")  # nobody checks an account's address
        voter = vote.Voter(kind=Ballot.Kind.ACCOUNT, user=sock, signed_in=sock, ip_hash="net-s")
        ballot, _ = vote.save_ballot(self.event, voter, {self.other.pk: 1})
        reasons = {f.ballot.pk: f.reasons for f in detect.ballot_flags(self.event)}
        self.assertTrue(any("account made" in r for r in reasons[ballot.pk]))

    def test_BS7_another_site_cannot_cast_a_ballot_in_a_voters_browser(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(make_user("victim@example.org"))
        url = reverse("voting:api_ballot", args=[self.event.slug])
        body = json.dumps({"votes": {str(self.other.pk): 1}})
        # A plain HTML form can only send form encodings, and a script on
        # another site can't send JSON with the voter's cookie without CORS.
        self.assertEqual(client.post(url, {"votes": body}).status_code, 415)
        self.assertEqual(client.post(url, body, content_type="application/json",
                                     HTTP_ORIGIN="https://evil.example").status_code, 403)
        self.assertEqual(client.post(reverse("voting:ballot", args=[self.event.slug]),
                                     {f"votes_{self.other.pk}": 1}).status_code, 403)  # the form needs its CSRF token
        self.assertFalse(Ballot.objects.exists())


class ScrapingTests(TestCase):
    def setUp(self):
        self.event = make_event(slug="open-hack", phase="open")
        self.team = make_team(self.event, make_user("secret.captain@example.org"), make_user("secret.mate@example.org"),
                              name="Early Birds")
        self.early = Project.objects.create(event=self.event, team=self.team, track=self.event.tracks.get(),
                                            title="Brilliant Early Idea", tagline="copy me", status="submitted",
                                            submitted_at=timezone.now(), repo_url="https://git.example/early")
        self.image = ProjectImage.objects.create(project=self.early, path=f"projects/{self.team.pk}/abc.png")

    def public_surfaces(self):
        c = Client()
        return {
            "gallery": c.get(reverse("projects:gallery")),
            "event gallery": c.get(reverse("projects:event_gallery", args=[self.event.slug])),
            "home": c.get("/"),
            "api list": c.get(V1 + "projects"),
            "old api list": c.get(reverse("projects:api_projects")),
            "embed feed": c.get(reverse("embeds:feed", args=[self.event.slug])),
            "embed widget": c.get(reverse("embeds:widget", args=[self.event.slug])),
        }

    def test_SC1_a_submission_stays_private_until_the_deadline(self):
        for name, page in self.public_surfaces().items():
            self.assertNotIn(b"Brilliant Early Idea", page.content, name)
        c = Client()
        self.assertEqual(c.get(self.early.get_absolute_url()).status_code, 404)
        self.assertEqual(c.get(f"{V1}projects/{self.early.pk}").status_code, 404)
        self.assertEqual(c.get(reverse("media", args=[self.image.path])).status_code, 404)
        rival = Client()
        rival.force_login(make_user("rival@example.org"))
        self.assertEqual(rival.get(self.early.get_absolute_url()).status_code, 404)
        self.assertEqual(rival.post(reverse("projects:comment_add", args=[self.early.pk]), {"body": "nice"}).status_code
                         in (302, 404), True)
        self.assertFalse(self.early.comments.exists())

    def test_SC1_the_team_and_organizers_still_see_it_and_everyone_does_at_the_deadline(self):
        mate = Client()
        mate.force_login(User.objects.get(email="secret.mate@example.org"))
        self.assertEqual(mate.get(self.early.get_absolute_url()).status_code, 200)
        Event.objects.filter(pk=self.event.pk).update(submissions_close_at=timezone.now())
        self.assertContains(Client().get(reverse("projects:gallery")), "Brilliant Early Idea")

    def test_SC2_a_draft_answers_exactly_like_a_project_that_does_not_exist(self):
        Project.objects.filter(pk=self.early.pk).update(status="draft")
        Event.objects.filter(pk=self.event.pk).update(submissions_close_at=timezone.now())
        c = Client()
        draft, missing = c.get(self.early.get_absolute_url()), c.get("/projects/987654/")
        self.assertEqual((draft.status_code, missing.status_code), (404, 404))
        # Identical apart from the sign-in link's return address, which is the URL that was asked for.
        self.assertEqual(draft.content.replace(self.early.get_absolute_url().encode(), b"/projects/987654/"),
                         missing.content)
        self.assertEqual(c.get(f"{V1}projects/{self.early.pk}").json(), c.get(f"{V1}projects/987654").json())

    def test_SC4_member_emails_are_nowhere_public(self):
        Event.objects.filter(pk=self.event.pk).update(submissions_close_at=timezone.now())
        surfaces = self.public_surfaces()
        c = Client()
        surfaces["project page"] = c.get(self.early.get_absolute_url())
        surfaces["api project"] = c.get(f"{V1}projects/{self.early.pk}")
        surfaces["event page"] = c.get(self.event.get_absolute_url())
        for name, page in surfaces.items():
            self.assertEqual(page.status_code, 200, name)
            self.assertNotIn(b"secret.captain@", page.content, name)
            self.assertNotIn(b"secret.mate@", page.content, name)


class JudgeTests(TestCase):
    def setUp(self):
        self.event = make_event(slug="judged", phase="closed")
        track = self.event.tracks.get()
        self.projects = [Project.objects.create(event=self.event, team=make_team(self.event, make_user(f"c{n}@example.org"),
                                                                                name=f"T{n}"),
                                                track=track, title=f"P{n}", status="submitted",
                                                submitted_at=self.event.submissions_close_at - timedelta(hours=1))
                         for n in range(6)]
        self.judges = [make_user(f"j{n}@example.org") for n in range(4)]
        for j in self.judges:
            add_staff(self.event, j, role=EventRole.Role.JUDGE)
        EventRole.objects.filter(event=self.event, role=EventRole.Role.JUDGE).update(all_tracks=True)
        self.criterion = Criterion.objects.create(event=self.event, key="impact", label="Impact", weight=1)
        self.batch = AssignmentBatch.objects.create(event=self.event, label="t", target_reviews=3)

    def score(self, judge, project, mark):
        Assignment.objects.get_or_create(event=self.event, judge=judge, project=project, defaults={"batch": self.batch})
        save_score(judge, self.event, project.pk, {self.criterion.pk: mark}, submit=True)

    def test_JC2_a_judge_cannot_open_or_score_a_project_they_were_not_given(self):
        judge = Client()
        judge.force_login(self.judges[0])
        self.score(self.judges[0], self.projects[0], 3)
        target = self.projects[5]
        self.assertEqual(judge.get(reverse("judging:score", args=[self.event.slug, target.pk])).status_code, 404)
        from judging.errors import NotAssigned
        with self.assertRaises(NotAssigned):
            save_score(self.judges[0], self.event, target.pk, {self.criterion.pk: 5}, submit=True)

    def test_JC3_a_judge_boosting_one_project_stands_apart_from_the_panel(self):
        marks = {0: [2, 3, 2, 3, 2, 3], 1: [3, 2, 3, 2, 3, 2], 2: [2, 2, 3, 3, 2, 2], 3: [3, 3, 2, 2, 3, 3]}
        friend = self.projects[4]
        for j, row in marks.items():
            for p, mark in zip(self.projects, row):
                if (j + self.projects.index(p)) % 4 == 3:
                    continue  # three judges per project
                self.score(self.judges[j], p, 5 if (j == 0 and p == friend) else mark)
        flagged = {g["project"].pk: g for g in detect.judge_disagreements(self.event)}
        self.assertIn(friend.pk, flagged)
        top = flagged[friend.pk]["judges"][0]
        self.assertEqual(top["judge"], self.judges[0])
        self.assertTrue(top["flagged"])

    def test_JC6_gap_staff_cannot_compete_as_themselves_but_a_second_account_can(self):
        team = self.projects[0].team
        Event.objects.filter(pk=self.event.pk).update(submissions_close_at=timezone.now() + timedelta(days=1))
        with self.assertRaises(TeamError):
            join_team(self.judges[0], team)
        join_team(make_user("j0.other.address@example.org"), team)  # the portal can't know it's the same person
        self.assertEqual(team.memberships.count(), 2)


class DeadlineTests(TestCase):
    def setUp(self):
        self.event = make_event(slug="late", phase="closed")
        self.captain = make_user("cap@example.org")
        self.team = make_team(self.event, self.captain)
        self.project = Project.objects.create(event=self.event, team=self.team, track=self.event.tracks.get(),
                                              title="Done", status="submitted",
                                              submitted_at=self.event.submissions_close_at - timedelta(minutes=5))
        self.organizer = make_user("org@example.org", role=User.Role.ORGANIZER)
        add_staff(self.event, self.organizer)

    def test_DG8_a_team_cannot_withdraw_or_add_images_after_the_deadline(self):
        with self.assertRaises(WindowError):
            withdraw_submission(self.captain, self.event, self.team)
        with self.assertRaises((WindowError, SubmissionError)):
            add_images(self.captain, self.event, self.team, [png_upload()])
        self.assertEqual(Project.objects.get().status, "submitted")

    def later(self, **extra):
        from events.forms import EventForm
        event = Event.objects.get(pk=self.event.pk)
        data = {f: getattr(event, f) for f in ("name", "slug", "timezone", "max_team_size", "is_published",
                                                "comments_enabled", "gallery_before_deadline")}
        tz = event.tzinfo
        for f in ("starts_at", "submissions_close_at"):
            data[f] = getattr(event, f).astimezone(tz).strftime("%Y-%m-%dT%H:%M")
        data["submissions_close_at"] = (timezone.now() + timedelta(days=1)).astimezone(tz).strftime("%Y-%m-%dT%H:%M")
        data.update(extra)
        form = EventForm(data, instance=event)
        self.assertTrue(form.is_valid(), form.errors)
        return event_services.update_event(self.organizer, event, form)

    def test_DG7_the_deadline_cannot_reopen_once_judges_have_scored(self):
        judge = make_user("judge@example.org")
        add_staff(self.event, judge, role=EventRole.Role.JUDGE)
        EventRole.objects.filter(user=judge).update(all_tracks=True)
        criterion = Criterion.objects.create(event=self.event, key="impact", label="Impact", weight=1)
        batch = AssignmentBatch.objects.create(event=self.event, label="t", target_reviews=1)
        Assignment.objects.create(event=self.event, judge=judge, project=self.project, batch=batch)
        save_score(judge, self.event, self.project.pk, {criterion.pk: 4}, submit=True)
        with self.assertRaises(event_services.EventError):
            self.later()
        self.assertLess(Event.objects.get(pk=self.event.pk).submissions_close_at, timezone.now())

    def test_DG7_before_anyone_judges_it_can_move_and_the_move_is_logged(self):
        self.later()
        self.assertGreater(Event.objects.get(pk=self.event.pk).submissions_close_at, timezone.now())
        self.assertTrue(self.event.activity.filter(verb="event.dates", detail__contains="the submission deadline").exists())

    def test_DG7_the_console_shows_the_refusal_instead_of_failing(self):
        Ballot.objects.create(event=self.event, kind="link")
        page = Client()
        page.force_login(self.organizer)
        url = reverse("events:manage_details", args=[self.event.slug])
        form = page.get(url).context["form"]
        data = {k: v for k, v in form.initial.items() if v is not None and k not in ("judging_ends_at", "results_at")}
        for f in ("starts_at", "submissions_close_at"):
            data[f] = form[f].value()
        data["submissions_close_at"] = (timezone.now() + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")
        response = page.post(url, data)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "can&#x27;t move later")
