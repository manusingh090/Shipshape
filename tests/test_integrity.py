"""Anti-abuse: rate limits, duplicate detection, and the audit trail."""

import json
from datetime import timedelta
from unittest import mock

from django.test import Client, SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from events.models import Activity, EventRole
from integrity import detect, limits
from projects.models import Comment, Project
from voting.models import Ballot, BallotEntry, VotingConfig
from voting.results import compute_tally

from .factories import add_staff, complete_submission, make_event, make_team, make_user


def tighter(scope, limit):
    """Shrink one limit for a test, keeping everything else about it."""
    old = limits.LIMITS[scope]
    return mock.patch.dict(limits.LIMITS, {scope: limits.Limit(old.scope, old.what, limit, old.window, old.per, old.why)})


class CanonicalEmailTests(SimpleTestCase):
    def test_aliases_that_reach_one_inbox_collapse(self):
        same = ["Ada.Lovelace@gmail.com", "adalovelace+vote2@googlemail.com", "a.d.a.lovelace@GMAIL.com"]
        self.assertEqual({detect.canonical_email(e) for e in same}, {"adalovelace@gmail.com"})
        self.assertEqual(detect.canonical_email("ada+x@uni.edu"), "ada@uni.edu")
        self.assertNotEqual(detect.canonical_email("a.da@uni.edu"), detect.canonical_email("ada@uni.edu"))


class Setup(TestCase):
    def setUp(self):
        self.event = make_event(slug="fair-hack", phase="closed")
        self.track = self.event.tracks.get()
        self.organizer = make_user("org@example.org", role=User.Role.ORGANIZER)
        add_staff(self.event, self.organizer)
        self.projects = [self.project(f"Project {n}", f"cap{n}@example.org") for n in range(4)]
        self.config = VotingConfig.objects.create(event=self.event, is_enabled=True,
                                                  access=VotingConfig.Access.ACCOUNT,
                                                  closes_at=timezone.now() + timedelta(days=1))

    def project(self, title, captain_email, repo=""):
        team = make_team(self.event, make_user(captain_email), name=f"{title} team")
        return Project.objects.create(event=self.event, team=team, track=self.track, title=title, tagline="x",
                                      repo_url=repo, status=Project.Status.SUBMITTED,
                                      submitted_at=self.event.submissions_close_at - timedelta(hours=1))

    def ballot(self, votes, email="", user=None, ip="net-a", ua="Firefox", at=None, kind="email"):
        at = at or timezone.now()
        b = Ballot.objects.create(event=self.event, kind=kind, email=email, user=user, ip_hash=ip, user_agent=ua,
                                  created_at=at, updated_at=at)
        for project, n in votes.items():
            BallotEntry.objects.create(ballot=b, project=project, votes=n)
        return b

    def client_for(self, user=None):
        client = Client()
        if user:
            client.force_login(user)
        return client

    def review(self, user, data=None):
        url = reverse("integrity:review", args=[self.event.slug])
        return self.client_for(user).post(url, data) if data is not None else self.client_for(user).get(url)


class DetectionTests(Setup):
    def reasons(self):
        return {f.ballot.pk: f.reasons for f in detect.ballot_flags(self.event)}

    def test_an_ordinary_ballot_raises_no_flag(self):
        b = self.ballot({self.projects[0]: 2, self.projects[1]: 1}, email="one@example.net")
        self.assertNotIn(b.pk, self.reasons())

    def test_a_burst_from_one_network_is_flagged(self):
        start = timezone.now() - timedelta(minutes=30)
        burst = [self.ballot({self.projects[n % 4]: 1}, email=f"b{n}@example.net", ua=f"UA{n}",
                             at=start + timedelta(minutes=n)) for n in range(5)]
        spread = self.ballot({self.projects[0]: 1}, email="late@example.net", ua="Other",
                             at=start + timedelta(hours=2))
        reasons = self.reasons()
        for b in burst:
            self.assertTrue(any("same network within 10 minutes" in r for r in reasons[b.pk]))
        self.assertNotIn(spread.pk, reasons)

    def test_same_device_copies_aliases_and_fresh_accounts(self):
        p = self.projects[0]
        copies = [self.ballot({p: 3}, email=f"fan{n}@example.net", ip=f"net-{n}", ua=f"UA{n}") for n in range(4)]
        device = [self.ballot({self.projects[1]: 1}, email=f"d{n}@example.net", ip="net-z", ua="Same UA")
                  for n in range(2)]
        alias = [self.ballot({self.projects[2]: 1}, email=e, ip=f"net-{e}", ua=e)
                 for e in ("ada.l@gmail.com", "adal+2@gmail.com")]
        newbie = make_user("new@example.org")
        fresh = self.ballot({self.projects[3]: 1}, user=newbie, kind="account", ip="net-new", ua="New")
        reasons = self.reasons()
        self.assertTrue(all(any("exactly the same votes as 3" in r for r in reasons[b.pk]) for b in copies))
        self.assertTrue(all(any("same network and browser" in r for r in reasons[b.pk]) for b in device))
        self.assertTrue(all(any("same inbox" in r for r in reasons[b.pk]) for b in alias))
        self.assertTrue(any("account made 0 minutes before voting" in r for r in reasons[fresh.pk]))

    def test_duplicate_projects_by_repo_or_title(self):
        a = self.project("Night Owl", "x@example.org", repo="https://github.com/Owls/night-owl.git")
        b = self.project("Something Else", "y@example.org", repo="http://www.github.com/owls/night-owl/")
        c = self.project("Night  owl!", "z@example.org")
        groups = detect.duplicate_projects(self.event)
        by_why = {g["why"]: {p.pk for p in g["projects"]} for g in groups}
        self.assertEqual(by_why["the same repository"], {a.pk, b.pk})
        self.assertEqual(by_why["the same title"], {a.pk, c.pk})

    def test_the_same_comment_on_three_projects_is_spam(self):
        spammer = make_user("spam@example.org")
        for p in self.projects[:3]:
            Comment.objects.create(project=p, author=spammer, body="Vote for Project 9 at bit.ly/xyz!!")
        Comment.objects.create(project=self.projects[3], author=spammer, body="An honest one-off comment.")
        groups = detect.repeated_comments(self.event)
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0]["comments"]), 3)


class DecisionTests(Setup):
    def test_organizers_leave_ballots_out_with_a_reason_and_can_undo_it(self):
        honest = self.ballot({self.projects[0]: 1}, email="honest@example.net", ip="n1")
        stuffed = [self.ballot({self.projects[1]: 3}, email=f"s{n}@example.net", ip="n2") for n in range(3)]
        self.assertEqual(compute_tally(self.event)["lines"][0]["project"], self.projects[1])

        voter = make_user("voter@example.org")
        self.assertEqual(self.review(voter).status_code, 403)
        self.assertEqual(self.review(voter, {"op": "exclude", "ballot": [stuffed[0].pk], "reason": "x"}).status_code, 403)
        self.review(self.organizer, {"op": "exclude", "ballot": [b.pk for b in stuffed], "reason": ""})
        self.assertFalse(Ballot.objects.filter(excluded_at__isnull=False).exists())  # a reason is required

        self.review(self.organizer, {"op": "exclude", "ballot": [b.pk for b in stuffed],
                                     "reason": "three ballots from one laptop in a minute"})
        tally = compute_tally(self.event)
        self.assertEqual(tally["lines"][0]["project"], self.projects[0])
        self.assertEqual((tally["voted"], tally["excluded"]), (1, 3))
        log = Activity.objects.get(verb="voting.ballot_excluded")
        self.assertIn("one laptop", log.detail)
        page = self.client_for(self.organizer).get(reverse("events:manage_activity", args=[self.event.slug]),
                                                   {"only": "integrity"})
        self.assertContains(page, "one laptop")

        self.review(self.organizer, {"op": "restore", "ballot": stuffed[0].pk})
        self.assertEqual(compute_tally(self.event)["voted"], 2)
        self.assertTrue(Activity.objects.filter(verb="voting.ballot_restored").exists())
        self.assertNotIn(honest.pk, Ballot.objects.filter(excluded_at__isnull=False).values_list("pk", flat=True))

    def test_the_export_says_which_ballots_count_and_why_they_were_flagged(self):
        b = self.ballot({self.projects[0]: 1}, email="ada.l@gmail.com", ip="n1", ua="A")
        self.ballot({self.projects[0]: 1}, email="adal+2@gmail.com", ip="n2", ua="B")
        self.review(self.organizer, {"op": "exclude", "ballot": [b.pk], "reason": "alias"})
        csv_text = self.client_for(self.organizer).get(
            reverse("judging:export", args=[self.event.slug, "ballots"])).content.decode()
        self.assertIn("counted,excluded_reason,flags", csv_text)
        self.assertIn("same inbox", csv_text)
        self.assertIn(",no,alias,", csv_text)

    def test_hiding_a_duplicate_project(self):
        copy = self.project("Project 0 again", "copy@example.org", repo="https://example.org/p0")
        Project.objects.filter(pk=self.projects[0].pk).update(repo_url="https://example.org/p0")
        page = self.review(self.organizer)
        self.assertContains(page, "the same repository")
        self.review(self.organizer, {"op": "hide_duplicate", "project": copy.pk, "original": self.projects[0].pk,
                                     "reason": "same repo, second team"})
        copy.refresh_from_db()
        self.assertEqual(copy.duplicate_of, self.projects[0])
        self.assertNotContains(Client().get(reverse("projects:gallery")), "Project 0 again")
        self.assertIn("same repo, second team", Activity.objects.get(verb="project.flagged").detail)

    def test_removing_repeated_comments_in_one_go(self):
        spammer = make_user("spam@example.org")
        spam = [Comment.objects.create(project=p, author=spammer, body="Buy followers now") for p in self.projects[:3]]
        self.review(self.organizer, {"op": "remove_comments", "comment": [c.pk for c in spam], "reason": "spam"})
        self.assertEqual(Comment.objects.filter(removed_at__isnull=False).count(), 3)
        self.assertEqual(Activity.objects.filter(verb="comment.removed").count(), 3)
        self.assertEqual(detect.repeated_comments(self.event), [])

    def test_the_page_shows_limits_and_what_it_found(self):
        for n in range(2):
            self.ballot({self.projects[1]: 1}, email=f"d{n}@example.net", ip="net-z", ua="Same UA")
        page = self.review(self.organizer)
        self.assertContains(page, "same network and browser")
        self.assertContains(page, "new accounts from one network".capitalize())
        judge = make_user("judge@example.org")
        EventRole.objects.create(event=self.event, user=judge, role=EventRole.Role.JUDGE)
        self.assertEqual(self.review(judge).status_code, 403)


class RateLimitTests(Setup):
    def test_signups_from_one_network_are_limited_and_logged_once(self):
        with tighter("signup.network", 2):
            for n in range(4):
                response = Client().post(reverse("accounts:signup"), {
                    "name": f"Person {n}", "email": f"p{n}@example.net",
                    "password1": "long enough pass 1", "password2": "long enough pass 1"})
        self.assertEqual(User.objects.filter(email__startswith="p", email__endswith="@example.net").count(), 2)
        self.assertEqual(response.status_code, 429)
        self.assertEqual(Activity.objects.filter(event__isnull=True, verb="rate.limited").count(), 1)

    def test_ballot_saves_and_new_ballots_per_network(self):
        voter = make_user("voter@example.org")
        client = self.client_for(voter)
        url = reverse("voting:api_ballot", args=[self.event.slug])
        post = lambda c, votes: c.post(url, data=json.dumps({"votes": votes}), content_type="application/json")
        with tighter("vote.save", 3):
            codes = [post(client, {self.projects[0].pk: 1}).status_code for _ in range(5)]
        self.assertEqual(codes, [200, 200, 200, 429, 429])
        with tighter("vote.new_ballot", 2):
            codes = [post(self.client_for(make_user(f"n{n}@example.org")), {self.projects[1].pk: 1}).status_code
                     for n in range(3)]
        self.assertEqual(codes, [200, 429, 429])  # the voter's ballot and this one make two from this network
        self.assertEqual(Activity.objects.filter(event=self.event, verb="rate.limited").count(), 2)

    def test_submission_saves_are_limited_on_the_api(self):
        event = make_event(slug="open-hack", phase="open")
        captain = make_user("builder@example.org")
        make_team(event, captain, name="Builders")
        client = self.client_for(captain)
        url = reverse("projects:api_submission", args=[event.slug])
        body = json.dumps({"action": "save", **complete_submission(event)})
        with tighter("submission.save", 2):
            codes = [client.post(url, data=body, content_type="application/json").status_code for _ in range(3)]
        self.assertEqual(codes[-1], 429)
        self.assertIn(codes[0], (200, 201))


class AuditTrailTests(Setup):
    def test_login_lockouts_and_admin_changes_reach_the_platform_log(self):
        victim = make_user("victim@example.org")
        for _ in range(6):
            Client().post(reverse("accounts:login"), {"email": "victim@example.org", "password": "wrong guess!"})
        admin = make_user("admin@example.org", role=User.Role.ADMIN)
        self.client_for(admin).post(reverse("accounts:admin_user_update", args=[victim.pk]),
                                    {"platform_role": "organizer", "is_active": "on"})
        page = self.client_for(admin).get(reverse("accounts:admin_audit"))
        self.assertContains(page, "sign-in for victim@example.org locked")
        self.assertContains(page, "made Victim (victim@example.org) organizer")
        self.assertContains(self.client_for(admin).get(reverse("accounts:admin")), "Platform audit log")
        self.assertEqual(self.client_for(self.organizer).get(reverse("accounts:admin_audit")).status_code, 403)
        search = self.client_for(admin).get(reverse("accounts:admin_audit"), {"q": "victim", "only": "accounts"})
        self.assertContains(search, "made Victim")
        self.assertNotContains(search, "locked")  # the lockout is under "refused", not "accounts"

    def test_the_event_log_filters_and_searches(self):
        from events.models import log_activity

        log_activity(self.event, self.organizer, "late.refused", "tried to edit after the deadline")
        log_activity(self.event, self.organizer, "judging.config", "changed the judging settings (kappa)")
        url = reverse("events:manage_activity", args=[self.event.slug])
        client = self.client_for(self.organizer)
        refused = client.get(url, {"only": "refused"})
        self.assertContains(refused, "after the deadline")
        self.assertNotContains(refused, "kappa")
        found = client.get(url, {"q": "kappa"})
        self.assertContains(found, "changed the judging settings")
        self.assertNotContains(found, "after the deadline")
