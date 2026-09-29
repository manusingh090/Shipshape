"""Community voting, through the same requests a browser or curl would make."""

import csv
import io
import json
import random
import re
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.core import mail
from django.test import Client, SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from events.models import EventRole
from events.seeding import Seeder, load_fixture
from projects.models import Project
from voting import method
from voting.models import Ballot, BallotEntry, EmailPass, VotingConfig
from voting.results import compute_tally

from .factories import add_staff, make_event, make_team, make_user


class MethodTests(SimpleTestCase):
    def test_votes_cost_their_square_and_the_ceiling_caps_them(self):
        self.assertEqual([method.cost(n) for n in (1, 2, 3, 5)], [1, 4, 9, 25])
        self.assertEqual(method.max_votes_per_project(25), 5)
        self.assertEqual(method.max_votes_per_project(25, cap=3), 3)
        self.assertEqual(method.max_votes_per_project(4, cap=3), 2)  # the budget is the tighter limit

    def test_a_quadratic_ballot_has_to_fit_the_budget_and_the_ceiling(self):
        self.assertEqual(method.check("quadratic", {1: 3, 2: 2, 3: 3}, 25, cap=3), 22)
        with self.assertRaisesRegex(method.BallotInvalid, "costs 27 credits"):
            method.check("quadratic", {1: 3, 2: 3, 3: 3}, 25, cap=3)
        with self.assertRaisesRegex(method.BallotInvalid, "3 votes is the most"):
            method.check("quadratic", {1: 4}, 25, cap=3)
        for bad in (0, -1, 1.5, True, "2"):
            with self.assertRaises(method.BallotInvalid):
                method.check("quadratic", {1: bad}, 25, cap=3)

    def test_one_person_one_vote_means_one_project(self):
        self.assertEqual(method.check("single", {7: 1}, 25), 1)
        self.assertEqual(method.check("single", {}, 25), 0)
        for bad in ({7: 1, 8: 1}, {7: 2}):
            with self.assertRaisesRegex(method.BallotInvalid, "pick one project"):
                method.check("single", bad, 25)

    def test_tally_ranks_by_votes_then_backers_with_shared_places(self):
        ballots = [{"a": 3}, {"b": 2}, {"b": 1}, {"c": 3}, {"d": 1}]
        lines = {line.project: line for line in method.tally(ballots, ["a", "b", "c", "d", "e"])}
        # b has 3 votes from two backers, so it beats a and c (3 votes, one backer each), who share second.
        self.assertEqual([lines[p].rank for p in "abcde"], [2, 1, 2, 4, 5])
        self.assertEqual((lines["b"].votes, lines["b"].supporters, lines["b"].credits), (3, 2, 5))
        self.assertEqual(lines["e"].votes, 0)

    def test_the_ceiling_is_what_keeps_a_bloc_out_of_the_top_three(self):
        """A small version of the simulation in JUDGING.md, section 10: a tenth
        of the voters back one weak project with everything they have."""
        rng = random.Random(3)
        top3 = {"one person, one vote": 0, "quadratic, ceiling": 0}
        for _ in range(40):
            result = method.one_event(rng, bloc_share=0.10)
            for name in top3:
                top3[name] += result[name]["bloc_top3"]
        self.assertGreater(top3["one person, one vote"], 10)
        self.assertEqual(top3["quadratic, ceiling"], 0)


class OrderMethodTests(SimpleTestCase):
    def test_a_seed_always_gives_the_same_order_and_seeds_differ(self):
        items = list(range(30))
        self.assertEqual(method.shuffled(items, b"a"), method.shuffled(items, b"a"))
        self.assertNotEqual(method.shuffled(items, b"a"), method.shuffled(items, b"b"))
        self.assertEqual(sorted(method.shuffled(items, b"a")), items)
        self.assertEqual(items, list(range(30)))  # the input isn't touched

    def test_one_shared_order_hands_the_podium_to_the_top_of_the_list(self):
        """A small version of JUDGING.md, section 10.5."""
        rng = random.Random(5)
        shared = sum(method.position_event(rng, False)["podium_from_top_quarter"] for _ in range(15)) / 15
        own = sum(method.position_event(rng, True)["podium_from_top_quarter"] for _ in range(15)) / 15
        self.assertGreater(shared, 0.7)
        self.assertLess(own, 0.5)


class VotingSetup(TestCase):
    """A closed event with three listed projects, one flagged duplicate, and a
    signed-in quadratic vote that is open now."""

    access = VotingConfig.Access.ACCOUNT

    def setUp(self):
        self.event = make_event(slug="voted-hack", phase="closed")
        self.track = self.event.tracks.get()
        self.organizer = make_user("org@example.org", role=User.Role.ORGANIZER)
        add_staff(self.event, self.organizer)
        self.captain = make_user("cap@example.org")
        self.p1 = self.project("Quiet Hours", self.captain)
        self.p2 = self.project("Loud Minutes", make_user("loud@example.org"))
        self.p3 = self.project("Warm Rivers", make_user("warm@example.org"))
        self.dupe = Project.objects.create(event=self.event, team=self.p2.team, track=self.track,
                                           title="Loud Minutes again", status=Project.Status.SUBMITTED,
                                           submitted_at=self.event.submissions_close_at, duplicate_of=self.p2)
        self.config = VotingConfig.objects.create(
            event=self.event, is_enabled=True, access=self.access,
            closes_at=timezone.now() + timedelta(days=2),
        )
        self.voter = make_user("voter@example.org")

    def project(self, title, captain):
        team = make_team(self.event, captain, name=f"{title} team")
        return Project.objects.create(event=self.event, team=team, track=self.track, title=title, tagline="x",
                                      status=Project.Status.SUBMITTED,
                                      submitted_at=self.event.submissions_close_at - timedelta(hours=1))

    def client_for(self, user=None):
        client = Client()
        if user:
            client.force_login(user)
        return client

    def api(self, client, votes, query=""):
        return client.post(reverse("voting:api_ballot", args=[self.event.slug]) + query,
                           data=json.dumps({"votes": votes}), content_type="application/json")


class AccountVotingTests(VotingSetup):
    def test_signed_out_people_meet_the_gate_not_the_ballot(self):
        response = Client().get(reverse("voting:ballot", args=[self.event.slug]))
        self.assertContains(response, "needs a Shipshape account")
        self.assertNotContains(response, 'name="p_')
        self.assertEqual(self.api(Client(), {self.p1.pk: 1}).status_code, 401)

    def test_a_member_votes_and_can_change_their_mind(self):
        client = self.client_for(self.voter)
        url = reverse("voting:ballot", args=[self.event.slug])
        response = client.post(url, {"op": "save", f"p_{self.p1.pk}": "3", f"p_{self.p2.pk}": "2"})
        self.assertRedirects(response, url)
        response = client.post(url, {"op": "save", f"p_{self.p3.pk}": "1"})
        ballot = Ballot.objects.get()
        self.assertEqual(ballot.user, self.voter)
        self.assertEqual(dict(ballot.entries.values_list("project_id", "votes")), {self.p3.pk: 1})

    def test_the_ballot_lists_what_the_gallery_lists(self):
        response = self.client_for(self.voter).get(reverse("voting:ballot", args=[self.event.slug]))
        for p in (self.p1, self.p2, self.p3):
            self.assertContains(response, f'name="p_{p.pk}"')
        self.assertNotContains(response, "Loud Minutes again")
        refused = self.api(self.client_for(self.voter), {self.dupe.pk: 1})
        self.assertEqual(refused.status_code, 400)
        self.assertFalse(BallotEntry.objects.exists())

    def test_the_budget_and_ceiling_hold_on_the_server(self):
        client = self.client_for(self.voter)
        for votes, message in [
            ({self.p1.pk: 3, self.p2.pk: 3, self.p3.pk: 3}, "27 credits"),
            ({self.p1.pk: 4}, "most one project"),
            ({self.p1.pk: -1}, "negative"),
            ({self.p1.pk: 2.5}, "whole numbers"),
            ({"not-a-project": 1}, "isn't on it"),
            ({999999: 1}, "isn't on it"),
        ]:
            response = self.api(client, votes)
            self.assertEqual(response.status_code, 400, votes)
            self.assertIn(message, response.json()["detail"])
        self.assertFalse(Ballot.objects.exists())
        response = self.api(client, {self.p1.pk: 3, self.p2.pk: 2, self.p3.pk: 3})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["ballot"]["credits_spent"], 22)

    def test_nobody_votes_for_their_own_team(self):
        client = self.client_for(self.captain)
        response = self.api(client, {self.p1.pk: 1})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"], "not_eligible")
        self.assertEqual(self.api(client, {self.p2.pk: 2}).status_code, 200)
        page = client.get(reverse("voting:ballot", args=[self.event.slug]))
        self.assertContains(page, "Your team's project.")

    def test_organizers_and_admins_cannot_vote_but_judges_can(self):
        admin = make_user("admin@example.org", role=User.Role.ADMIN)
        judge = make_user("judge@example.org")
        EventRole.objects.create(event=self.event, user=judge, role=EventRole.Role.JUDGE)
        for user in (self.organizer, admin):
            response = self.api(self.client_for(user), {self.p1.pk: 1})
            self.assertEqual(response.status_code, 403, user)
            self.assertIn("can see the count", response.json()["detail"])
        self.assertEqual(self.api(self.client_for(judge), {self.p1.pk: 1}).status_code, 200)

    def test_the_window_is_the_servers(self):
        client = self.client_for(self.voter)
        self.config.closes_at = timezone.now()  # the closing instant counts as closed
        self.config.save()
        response = self.api(client, {self.p1.pk: 1})
        self.assertEqual((response.status_code, response.json()["error"]), (403, "voting_closed"))
        page = client.post(reverse("voting:ballot", args=[self.event.slug]), {"op": "save", f"p_{self.p1.pk}": "1"})
        self.assertContains(page, "Not saved.")
        self.config.opens_at = timezone.now() + timedelta(hours=1)
        self.config.closes_at = timezone.now() + timedelta(days=1)
        self.config.save()
        response = self.api(client, {self.p1.pk: 1})
        self.assertEqual(response.json()["error"], "voting_not_open")
        self.assertFalse(Ballot.objects.exists())

    def test_a_vote_that_is_off_has_no_ballot_page(self):
        self.config.is_enabled = False
        self.config.save()
        client = self.client_for(self.voter)
        self.assertEqual(client.get(reverse("voting:ballot", args=[self.event.slug])).status_code, 404)
        self.assertEqual(self.api(client, {self.p1.pk: 1}).status_code, 404)
        self.assertNotContains(client.get(self.event.get_absolute_url()), "Community vote")

    def test_a_flagged_duplicate_drops_out_of_the_count(self):
        self.api(self.client_for(self.voter), {self.p2.pk: 3})
        self.assertEqual(compute_tally(self.event)["lines"][0]["project"], self.p2)
        self.p2.duplicate_of = self.p3
        self.p2.save()
        self.assertNotIn(self.p2, [line["project"] for line in compute_tally(self.event)["lines"]])
        self.assertTrue(BallotEntry.objects.filter(project=self.p2).exists())  # kept, just not counted


class SingleVoteTests(VotingSetup):
    def setUp(self):
        super().setUp()
        self.config.method = VotingConfig.Method.SINGLE
        self.config.save()

    def test_one_pick_and_only_one(self):
        client = self.client_for(self.voter)
        url = reverse("voting:ballot", args=[self.event.slug])
        self.assertRedirects(client.post(url, {"op": "save", "pick": str(self.p3.pk)}), url)
        self.assertEqual(list(BallotEntry.objects.values_list("project_id", "votes")), [(self.p3.pk, 1)])
        self.assertEqual(self.api(client, {self.p1.pk: 1, self.p2.pk: 1}).status_code, 400)
        client.post(url, {"op": "save", "pick": ""})
        self.assertFalse(BallotEntry.objects.exists())


class EmailVotingTests(VotingSetup):
    access = VotingConfig.Access.EMAIL

    def ask(self, client, email):
        return client.post(reverse("voting:ballot", args=[self.event.slug]), {"op": "email", "email": email})

    def link_in_mail(self, index=-1):
        return re.search(r"http://testserver(/\S+)", mail.outbox[index].body).group(1)

    def test_a_link_by_email_opens_a_ballot_tied_to_the_address(self):
        client = Client()
        self.assertContains(self.ask(client, " Visitor@Example.NET "), "visitor@example.net")
        self.assertEqual(mail.outbox[0].to, ["visitor@example.net"])
        link = self.link_in_mail()
        self.assertContains(client.get(link), "This uses up the link.")  # looking doesn't use it up
        self.assertFalse(EmailPass.objects.get().used_at)
        self.assertRedirects(client.post(link), reverse("voting:ballot", args=[self.event.slug]))
        self.assertEqual(self.api(client, {self.p1.pk: 2}).status_code, 200)
        self.assertEqual(Ballot.objects.get().email, "visitor@example.net")

        # The link worked once. A new link, on another device, reaches the same ballot.
        self.assertContains(Client().post(link), "used already")
        other = Client()
        self.ask(other, "visitor@example.net")
        other.post(self.link_in_mail())
        self.assertEqual(self.api(other, {self.p3.pk: 1}).status_code, 200)
        self.assertEqual(Ballot.objects.count(), 1)
        self.assertEqual(list(BallotEntry.objects.values_list("project_id", flat=True)), [self.p3.pk])

    def test_no_ballot_without_confirming(self):
        client = Client()
        self.ask(client, "visitor@example.net")
        self.assertEqual(self.api(client, {self.p1.pk: 1}).status_code, 401)
        self.assertNotContains(client.get(reverse("voting:ballot", args=[self.event.slug])), 'name="p_')

    def test_only_the_hash_of_the_token_is_stored(self):
        self.ask(Client(), "visitor@example.net")
        token = self.link_in_mail().rstrip("/").rsplit("/", 1)[-1]
        self.assertNotEqual(EmailPass.objects.get().token_hash, token)
        self.assertEqual(len(EmailPass.objects.get().token_hash), 64)

    def test_links_expire(self):
        client = Client()
        self.ask(client, "visitor@example.net")
        EmailPass.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
        self.assertContains(client.post(self.link_in_mail()), "expired")

    def test_domains_can_be_restricted(self):
        self.config.email_domains = "example.org"
        self.config.save()
        self.assertContains(self.ask(Client(), "someone@elsewhere.net"), "open to addresses at example.org")
        self.assertEqual(len(mail.outbox), 0)
        self.ask(Client(), "someone@example.org")
        self.assertEqual(len(mail.outbox), 1)

    def test_asking_for_links_is_rate_limited(self):
        client = Client()
        for _ in range(3):
            self.ask(client, "visitor@example.net")
        self.assertContains(self.ask(client, "visitor@example.net"), "a few links already")
        self.assertEqual(len(mail.outbox), 3)

    def test_account_rules_follow_the_address(self):
        self.assertContains(self.ask(Client(), "org@example.org"), "can see the count")
        client = Client()
        self.ask(client, "cap@example.org")
        client.post(self.link_in_mail())
        self.assertEqual(self.api(client, {self.p1.pk: 1}).status_code, 403)  # their own team
        self.assertEqual(self.api(client, {self.p2.pk: 1}).status_code, 200)


    def test_signing_in_as_staff_blocks_an_email_ballot_too(self):
        client = Client()
        self.ask(client, "visitor@example.net")
        client.post(self.link_in_mail())
        client.force_login(self.organizer)  # the confirmed address is still in the session...
        self.assertEqual(self.api(client, {self.p1.pk: 1}).status_code, 401)  # ...but isn't theirs
        # And an organizer can't get a link for their own address either.
        self.assertContains(self.ask(self.client_for(self.organizer), "org@example.org"), "can see the count")
        self.assertFalse(Ballot.objects.exists())

    def test_signed_in_you_vote_with_your_own_address(self):
        client = self.client_for(self.captain)
        gate = client.get(reverse("voting:ballot", args=[self.event.slug]))
        self.assertContains(gate, "cap@example.org")
        self.assertNotContains(gate, 'name="email"')  # nothing to type
        self.ask(client, "somebody.else@example.net")  # a hand-made POST can't change the address
        self.assertEqual(mail.outbox[-1].to, ["cap@example.org"])
        client.post(self.link_in_mail())
        self.assertEqual(self.api(client, {self.p1.pk: 1}).status_code, 403)  # still their own team
        self.assertEqual(self.api(client, {self.p2.pk: 2}).status_code, 200)
        self.assertEqual(Ballot.objects.get().email, "cap@example.org")
        self.assertNotContains(client.get(reverse("voting:ballot", args=[self.event.slug])), "Use a different address")

    def test_a_link_for_another_address_does_not_open_while_signed_in(self):
        self.ask(Client(), "visitor@example.net")
        link = self.link_in_mail()
        client = self.client_for(self.voter)
        self.assertContains(client.get(link), "signed in as voter@example.org")
        self.assertContains(client.post(link), "signed in as voter@example.org")
        self.assertIsNone(EmailPass.objects.get().used_at)  # not used up: its owner can still open it
        self.assertEqual(self.api(client, {self.p1.pk: 1}).status_code, 401)
        self.assertRedirects(Client().post(link), reverse("voting:ballot", args=[self.event.slug]))

    def test_the_service_refuses_a_mismatched_address_on_its_own(self):
        from voting.services import VoteError, request_email_pass

        with self.assertRaisesRegex(VoteError, "signed in as voter@example.org"):
            request_email_pass(self.event, "other@example.net", "", lambda t: t, signed_in=self.voter)
        self.assertEqual(len(mail.outbox), 0)


class LinkVotingTests(VotingSetup):
    access = VotingConfig.Access.LINK

    def link(self):
        return reverse("voting:ballot_link", args=[self.event.slug, self.config.link_token])

    def test_the_plain_address_needs_the_link(self):
        response = Client().get(reverse("voting:ballot", args=[self.event.slug]))
        self.assertContains(response, "by invitation")
        self.assertNotContains(response, self.config.link_token)
        self.assertEqual(Client().get(reverse("voting:ballot_link", args=[self.event.slug, "guess"])).status_code, 404)
        self.assertNotContains(Client().get(self.event.get_absolute_url()), self.config.link_token)

    def test_one_ballot_per_browser_that_opened_the_link(self):
        client = Client()
        self.assertRedirects(client.post(self.link(), {"op": "save", f"p_{self.p1.pk}": "2"}), self.link())
        client.post(self.link(), {"op": "save", f"p_{self.p2.pk}": "1"})
        self.assertEqual(Ballot.objects.count(), 1)
        self.assertEqual(Ballot.objects.get().kind, "link")
        self.assertEqual(len(Ballot.objects.get().ip_hash), 64)
        # "Use a different address" exists only for email ballots; here it would just start a second one.
        client.post(self.link(), {"op": "forget"})
        client.post(self.link(), {"op": "save", f"p_{self.p3.pk}": "1"})
        self.assertEqual(Ballot.objects.count(), 1)

    def test_a_new_link_retires_the_old_one(self):
        old = self.link()
        self.client_for(self.organizer).post(reverse("voting:manage", args=[self.event.slug]), {"op": "new_link"})
        self.config.refresh_from_db()
        self.assertEqual(Client().get(old).status_code, 404)
        self.assertEqual(Client().get(self.link()).status_code, 200)

    def test_the_api_needs_the_link_too(self):
        client = Client()
        self.assertEqual(self.api(client, {self.p1.pk: 1}).status_code, 404)
        self.assertEqual(self.api(client, {self.p1.pk: 1}, f"?link={self.config.link_token}").status_code, 200)

    def test_organizers_cannot_vote_through_the_link_while_signed_in(self):
        response = self.client_for(self.organizer).post(self.link(), {"op": "save", f"p_{self.p1.pk}": "1"})
        self.assertContains(response, "can see the count")
        self.assertFalse(Ballot.objects.exists())

    def test_signed_in_voters_still_cannot_back_their_own_team(self):
        client = self.client_for(self.captain)
        response = client.post(self.link(), {"op": "save", f"p_{self.p1.pk}": "1"})
        self.assertContains(response, "own team")
        self.assertFalse(BallotEntry.objects.exists())


class OrganizerTests(VotingSetup):
    def manage_url(self):
        return reverse("voting:manage", args=[self.event.slug])

    def test_only_organizers_get_the_voting_page(self):
        judge = make_user("judge@example.org")
        EventRole.objects.create(event=self.event, user=judge, role=EventRole.Role.JUDGE)
        for user in (self.voter, self.captain, judge):
            self.assertEqual(self.client_for(user).get(self.manage_url()).status_code, 403)
            self.assertEqual(self.client_for(user).post(self.manage_url(), {"op": "new_link"}).status_code, 403)
        self.assertEqual(self.client_for(self.organizer).get(self.manage_url()).status_code, 200)

    def settings_post(self, **changes):
        data = {"op": "config", "is_enabled": "on", "access": "account", "method": "quadratic", "credits": "25",
                "max_votes": "3", "order": "shuffled", "opens_at": "", "email_domains": "",
                "closes_at": (timezone.now() + timedelta(days=3)).strftime("%Y-%m-%dT%H:%M")}
        data.update(changes)
        return self.client_for(self.organizer).post(self.manage_url(), data)

    def test_rules_lock_once_a_ballot_is_in(self):
        self.assertEqual(self.settings_post(access="email", credits="36").status_code, 302)
        self.config.refresh_from_db()
        self.assertEqual((self.config.access, self.config.credits), ("email", 36))
        self.config.access = "account"
        self.config.save()
        self.api(self.client_for(self.voter), {self.p1.pk: 1})
        self.settings_post(access="link", method="single", credits="100", max_votes="10")
        self.config.refresh_from_db()
        self.assertEqual((self.config.access, self.config.method, self.config.credits, self.config.max_votes),
                         ("account", "quadratic", 36, 3))

    def test_the_window_has_to_make_sense(self):
        before_deadline = (self.event.submissions_close_at - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M")
        self.assertContains(self.settings_post(opens_at=before_deadline), "before submissions close")
        self.assertContains(self.settings_post(closes_at=""), "Set a closing time")

    def test_exports_are_organizer_only_and_add_up(self):
        self.api(self.client_for(self.voter), {self.p1.pk: 3, self.p2.pk: 1})
        self.api(self.client_for(make_user("second@example.org")), {self.p2.pk: 3})
        for stage in ("community-votes", "ballots"):
            url = reverse("judging:export", args=[self.event.slug, stage])
            self.assertEqual(self.client_for(self.voter).get(url).status_code, 403)
        url = reverse("judging:export", args=[self.event.slug, "community-votes"])
        rows = list(csv.DictReader(io.StringIO(self.client_for(self.organizer).get(url).content.decode())))
        self.assertEqual([(r["title"], r["rank"], r["votes"], r["backers"]) for r in rows[:2]],
                         [("Loud Minutes", "1", "4", "2"), ("Quiet Hours", "2", "3", "1")])
        url = reverse("judging:export", args=[self.event.slug, "ballots"])
        rows = list(csv.DictReader(io.StringIO(self.client_for(self.organizer).get(url).content.decode())))
        self.assertEqual(len(rows), 3)
        self.assertIn("voter@example.org", {r["voter"] for r in rows})


class HiddenResultsTests(VotingSetup):
    """Results are hidden from everyone but organizers while voting is open,
    and stay hidden after it closes until an organizer reviews and publishes."""

    def setUp(self):
        super().setUp()
        self.api(self.client_for(self.voter), {self.p3.pk: 3})
        self.judge = make_user("judge@example.org")
        EventRole.objects.create(event=self.event, user=self.judge, role=EventRole.Role.JUDGE)
        self.admin = make_user("admin@example.org", role=User.Role.ADMIN)
        self.outsiders = [None, self.voter, self.captain, self.judge]

    def results_page(self, user=None):
        return self.client_for(user).get(reverse("voting:results", args=[self.event.slug]))

    def results_api(self, user=None):
        return self.client_for(user).get(reverse("voting:api_results", args=[self.event.slug]))

    def close(self):
        self.config.closes_at = timezone.now() - timedelta(minutes=1)
        self.config.save()

    def assert_hidden(self, state):
        for user in self.outsiders:
            page = self.results_page(user)
            self.assertEqual(page.status_code, 200)
            self.assertNotContains(page, "Warm Rivers")  # the leader, by name
            self.assertNotContains(page, "people voted")  # not even turnout
            response = self.results_api(user)
            self.assertEqual((response.status_code, response.json()["error"], response.json()["state"]),
                             (403, "results_hidden", state))
            self.assertNotIn("Warm Rivers", response.content.decode())
            ballot = self.client_for(user).get(reverse("voting:ballot", args=[self.event.slug]))
            self.assertNotContains(ballot, "Backers")
            url = reverse("judging:export", args=[self.event.slug, "community-votes"])
            self.assertIn(self.client_for(user).get(url).status_code, (302, 401, 403))

    def test_hidden_from_everyone_but_organizers_while_voting_is_open(self):
        self.assert_hidden("hidden")
        for organizer in (self.organizer, self.admin):
            page = self.results_page(organizer)
            self.assertContains(page, "Organizer preview")
            self.assertContains(page, "Warm Rivers")
            self.assertEqual(self.results_api(organizer).json()["results"][0]["title"], "Warm Rivers")

    def test_closing_is_not_enough_an_organizer_has_to_publish(self):
        self.close()
        self.assert_hidden("awaiting_review")
        self.assertContains(self.results_page(), "looking over the ballots")
        manage = reverse("voting:manage", args=[self.event.slug])
        self.assertEqual(self.client_for(self.voter).post(manage, {"op": "publish"}).status_code, 403)
        self.client_for(self.organizer).post(manage, {"op": "publish"})
        for user in self.outsiders:
            self.assertContains(self.results_page(user), "Warm Rivers")
            self.assertEqual(self.results_api(user).json()["state"], "published")
        self.assertContains(Client().get(self.event.get_absolute_url()), reverse("voting:results", args=[self.event.slug]))

    def test_results_cannot_be_published_while_voting_is_open(self):
        self.client_for(self.organizer).post(reverse("voting:manage", args=[self.event.slug]), {"op": "publish"})
        self.config.refresh_from_db()
        self.assertIsNone(self.config.results_published_at)
        self.assert_hidden("hidden")

    def test_reopening_voting_takes_published_results_down(self):
        self.close()
        manage = reverse("voting:manage", args=[self.event.slug])
        self.client_for(self.organizer).post(manage, {"op": "publish"})
        reopen = {"op": "config", "is_enabled": "on", "access": "account", "method": "quadratic",
                  "credits": "25", "max_votes": "3", "order": "shuffled", "opens_at": "", "email_domains": "",
                  "closes_at": (timezone.now() + timedelta(days=1)).astimezone(self.event.tzinfo).strftime("%Y-%m-%dT%H:%M")}
        self.client_for(self.organizer).post(manage, reopen)
        self.config.refresh_from_db()
        self.assertIsNone(self.config.results_published_at)
        self.assert_hidden("hidden")


class BallotOrderTests(VotingSetup):
    def setUp(self):
        super().setUp()
        for n in range(9):
            self.project(f"Extra {n:02d}", make_user(f"extra{n}@example.org"))
        self.titles = sorted(p.title for p in Project.objects.filter(event=self.event, duplicate_of__isnull=True))

    def order_for(self, client, query=""):
        response = client.get(reverse("voting:api_ballot", args=[self.event.slug]) + query)
        self.assertEqual(response.status_code, 200)
        return [p["title"] for p in response.json()["projects"]]

    def page_order(self, client, url=None):
        html = client.get(url or reverse("voting:ballot", args=[self.event.slug])).content.decode()
        return re.findall(r'<p class="ballot-title"><a [^>]*>([^<]+)</a>', html)

    def test_every_voter_gets_their_own_order_and_keeps_it(self):
        voters = [self.voter] + [make_user(f"v{n}@example.org") for n in range(4)]
        orders = []
        for user in voters:
            client = self.client_for(user)
            first = self.order_for(client)
            self.assertEqual(sorted(first), self.titles)       # every project, once
            self.assertEqual(self.order_for(client), first)    # the same when they come back
            self.assertEqual(self.page_order(client), first)   # the page and the API agree
            orders.append(tuple(first))
        self.assertGreater(len(set(orders)), 3)
        # Voting doesn't reshuffle it.
        client = self.client_for(self.voter)
        self.api(client, {self.p1.pk: 1})
        self.assertEqual(tuple(self.order_for(client)), orders[0])

    def test_organizers_can_choose_a_to_z_before_voting_starts(self):
        self.config.order = VotingConfig.Order.ALPHABETICAL
        self.config.save()
        for user in (self.voter, make_user("other@example.org")):
            self.assertEqual(self.order_for(self.client_for(user)), self.titles)

    def test_an_open_link_voter_keeps_their_order_through_saving(self):
        self.config.access = VotingConfig.Access.LINK
        self.config.save()
        link = reverse("voting:ballot_link", args=[self.event.slug, self.config.link_token])
        client = Client()
        before = self.page_order(client, link)
        self.assertEqual(self.page_order(client, link), before)
        client.post(link, {"op": "save", f"p_{self.p1.pk}": "1"})
        self.assertEqual(self.page_order(client, link), before)
        others = {tuple(self.page_order(Client(), link)) for _ in range(4)}
        self.assertGreater(len(others | {tuple(before)}), 2)

    def test_an_email_voter_sees_the_same_order_on_any_device(self):
        self.config.access = VotingConfig.Access.EMAIL
        self.config.save()
        orders = []
        for _ in range(2):
            client = Client()
            client.post(reverse("voting:ballot", args=[self.event.slug]), {"op": "email", "email": "fan@example.net"})
            client.post(re.search(r"http://testserver(/\S+)", mail.outbox[-1].body).group(1))
            orders.append(self.page_order(client))
        self.assertEqual(orders[0], orders[1])
        self.assertEqual(sorted(orders[0]), self.titles)


class SeedTests(TestCase):
    def test_demo_seed_sets_up_a_vote_on_each_event_once(self):
        fixture = load_fixture(Path(settings.BASE_DIR) / "fixtures.json")
        Seeder(demo=True, password="dogfood-demo").run(fixture)
        Seeder(demo=True, password="dogfood-demo").run(fixture)
        configs = {c.event.slug: c for c in VotingConfig.objects.select_related("event")}
        self.assertEqual(set(configs), {"sample-hack-2026", "autumn-build-weekend"})
        fixture_vote = configs["sample-hack-2026"]
        self.assertEqual((fixture_vote.access, fixture_vote.method, fixture_vote.credits, fixture_vote.max_votes),
                         ("email", "quadratic", 25, 3))
        self.assertFalse(Ballot.objects.exists())
        response = Client().get(reverse("events:detail", args=["sample-hack-2026"]))
        self.assertContains(response, reverse("voting:ballot", args=["sample-hack-2026"]))
