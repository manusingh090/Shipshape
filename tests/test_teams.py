from django.test import TestCase
from django.urls import reverse

from events.models import EventRole
from teams.models import Membership, Team

from .factories import add_staff, make_event, make_team, make_user


class InviteTests(TestCase):
    def setUp(self):
        self.event = make_event(max_team_size=3)
        self.priya = make_user("priya@example.org")
        self.client.force_login(self.priya)
        self.client.post(reverse("teams:mine", args=[self.event.slug]), {"name": "Night Owls"})
        self.team = Team.objects.get(event=self.event)

    def join_as(self, user, code=None):
        self.client.force_login(user)
        return self.client.post(reverse("teams:join", args=[code or self.team.invite_code]))

    def test_creator_is_captain(self):
        membership = self.team.memberships.get(user=self.priya)
        self.assertEqual(membership.role, Membership.Role.CAPTAIN)

    def test_invite_link_adds_a_member(self):
        tom = make_user("tom@example.org")
        response = self.join_as(tom)
        self.assertRedirects(response, reverse("teams:mine", args=[self.event.slug]))
        self.assertTrue(self.team.memberships.filter(user=tom).exists())

    def test_anonymous_invite_visit_asks_to_sign_in(self):
        self.client.logout()
        response = self.client.get(reverse("teams:join", args=[self.team.invite_code]))
        self.assertContains(response, "Sign in")
        response = self.client.post(reverse("teams:join", args=[self.team.invite_code]))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("accounts:login"), response["Location"])

    def test_full_team_refuses(self):
        for n in range(2):
            self.join_as(make_user(f"member{n}@example.org"))
        extra = make_user("extra@example.org")
        self.join_as(extra)
        self.assertEqual(self.team.memberships.count(), 3)
        self.assertFalse(self.team.memberships.filter(user=extra).exists())

    def test_one_team_per_person_per_event(self):
        tom = make_user("tom@example.org")
        make_team(self.event, tom, name="Other Team")
        self.join_as(tom)
        self.assertFalse(self.team.memberships.filter(user=tom).exists())

    def test_judges_cannot_join_a_team_in_their_event(self):
        judge = make_user("judge@example.org")
        add_staff(self.event, judge, EventRole.Role.JUDGE)
        self.join_as(judge)
        self.assertFalse(self.team.memberships.filter(user=judge).exists())

    def test_platform_admins_cannot_compete(self):
        from accounts.models import User
        admin = make_user("admin@example.org", role=User.Role.ADMIN)
        self.join_as(admin)
        self.assertFalse(self.team.memberships.filter(user=admin).exists())
        self.client.post(reverse("teams:mine", args=[self.event.slug]), {"name": "Admins United"})
        self.assertFalse(admin.memberships.exists())

    def test_new_invite_link_kills_the_old_one(self):
        old_code = self.team.invite_code
        self.client.post(reverse("teams:reset_invite", args=[self.event.slug]))
        self.team.refresh_from_db()
        self.assertNotEqual(self.team.invite_code, old_code)
        response = self.client.get(reverse("teams:join", args=[old_code]))
        self.assertEqual(response.status_code, 404)

    def test_only_the_captain_can_reset_or_remove(self):
        tom = make_user("tom@example.org")
        self.join_as(tom)
        code = self.team.invite_code
        self.client.post(reverse("teams:reset_invite", args=[self.event.slug]))
        self.client.post(reverse("teams:remove_member", args=[self.event.slug, self.priya.pk]))
        self.team.refresh_from_db()
        self.assertEqual(self.team.invite_code, code)
        self.assertTrue(self.team.memberships.filter(user=self.priya).exists())

    def test_captaincy_passes_on_and_last_one_out_dissolves(self):
        tom = make_user("tom@example.org")
        self.join_as(tom)
        self.client.force_login(self.priya)
        self.client.post(reverse("teams:leave", args=[self.event.slug]))
        self.assertEqual(self.team.memberships.get(user=tom).role, Membership.Role.CAPTAIN)
        self.client.force_login(tom)
        self.client.post(reverse("teams:leave", args=[self.event.slug]))
        self.assertFalse(Team.objects.filter(pk=self.team.pk).exists())
