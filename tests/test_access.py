"""Roles: visitor, participant, judge, organizer, admin."""

from django.test import TestCase
from django.urls import reverse

from accounts.models import User
from events.models import Event, EventRole

from .factories import add_staff, make_event, make_team, make_user


class RoleTests(TestCase):
    def setUp(self):
        self.event = make_event()
        self.organizer = make_user("org@example.org", role=User.Role.ORGANIZER)
        add_staff(self.event, self.organizer)
        self.judge = make_user("judge@example.org")
        add_staff(self.event, self.judge, EventRole.Role.JUDGE)
        self.participant = make_user("priya@example.org")
        make_team(self.event, self.participant)
        self.admin = make_user("admin@example.org", role=User.Role.ADMIN)
        self.manage_urls = [
            reverse(name, args=[self.event.slug]) for name in (
                "events:manage", "events:manage_details", "events:manage_tracks", "events:manage_prizes",
                "events:manage_questions", "events:manage_people", "events:manage_submissions",
                "events:manage_teams", "events:manage_activity",
            )
        ]

    def test_visitors_are_sent_to_sign_in(self):
        for url in self.manage_urls:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 302, url)
            self.assertIn(reverse("accounts:login"), response["Location"])

    def test_participants_and_judges_are_refused_the_console(self):
        for user in (self.participant, self.judge):
            self.client.force_login(user)
            for url in self.manage_urls:
                self.assertEqual(self.client.get(url).status_code, 403, f"{user} {url}")

    def test_organizers_and_admins_get_in(self):
        for user in (self.organizer, self.admin):
            self.client.force_login(user)
            for url in self.manage_urls:
                self.assertEqual(self.client.get(url).status_code, 200, f"{user} {url}")

    def test_console_writes_are_refused_too(self):
        self.client.force_login(self.participant)
        response = self.client.post(reverse("events:manage_tracks", args=[self.event.slug]),
                                    {"op": "save", "name": "Sneaky track"})
        self.assertEqual(response.status_code, 403)
        self.assertFalse(self.event.tracks.filter(name="Sneaky track").exists())

    def test_creating_an_event_needs_the_organizer_role(self):
        self.client.force_login(self.participant)
        self.assertEqual(self.client.get(reverse("events:create")).status_code, 403)
        self.client.force_login(self.organizer)
        response = self.client.post(reverse("events:create"), {
            "name": "Spring Hack", "timezone": "Europe/Lisbon",
            "starts_at": "2030-04-10T18:00", "submissions_close_at": "2030-04-13T18:00",
            "max_team_size": 4, "tracks_text": "Climate\nHealth\nclimate",
        })
        event = Event.objects.get(slug="spring-hack")
        self.assertRedirects(response, reverse("events:manage", args=[event.slug]))
        self.assertEqual(list(event.tracks.values_list("name", flat=True)), ["Climate", "Health"])
        self.assertTrue(EventRole.objects.filter(event=event, user=self.organizer, role="organizer").exists())
        # 18:00 in Lisbon (WEST, UTC+1 in April) is 17:00 UTC.
        self.assertEqual(event.submissions_close_at.hour, 17)
        self.assertFalse(event.is_published)

    def test_unpublished_events_are_invisible_to_the_public(self):
        hidden = make_event(slug="hidden-hack", is_published=False)
        self.assertEqual(self.client.get(hidden.get_absolute_url()).status_code, 404)
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(hidden.get_absolute_url()).status_code, 200)

    def test_organizers_add_judges_but_not_competitors(self):
        self.client.force_login(self.organizer)
        newcomer = make_user("new.judge@example.org")
        url = reverse("judging:judges", args=[self.event.slug])
        self.client.post(url, {"op": "add", "email": "New.Judge@example.org"})
        self.assertTrue(EventRole.objects.filter(event=self.event, user=newcomer, role="judge").exists())
        self.client.post(url, {"op": "add", "email": self.participant.email})
        self.assertFalse(EventRole.objects.filter(event=self.event, user=self.participant).exists())

    def test_people_tab_adds_organizers(self):
        self.client.force_login(self.organizer)
        colleague = make_user("colleague@example.org")
        self.client.post(reverse("events:manage_people", args=[self.event.slug]), {"email": colleague.email})
        self.assertTrue(EventRole.objects.filter(event=self.event, user=colleague, role="organizer").exists())


class AdminTests(TestCase):
    def setUp(self):
        self.admin = make_user("admin@example.org", role=User.Role.ADMIN)
        self.member = make_user("member@example.org")
        self.client.force_login(self.admin)

    def test_admin_page_is_admin_only(self):
        self.assertEqual(self.client.get(reverse("accounts:admin")).status_code, 200)
        self.client.force_login(self.member)
        self.assertEqual(self.client.get(reverse("accounts:admin")).status_code, 403)
        response = self.client.post(reverse("accounts:admin_user_update", args=[self.member.pk]),
                                    {"platform_role": "admin", "is_active": "on"})
        self.assertEqual(response.status_code, 403)
        self.member.refresh_from_db()
        self.assertEqual(self.member.platform_role, User.Role.MEMBER)

    def test_admin_promotes_and_deactivates(self):
        url = reverse("accounts:admin_user_update", args=[self.member.pk])
        self.client.post(url, {"platform_role": "organizer", "is_active": "on"})
        self.member.refresh_from_db()
        self.assertEqual(self.member.platform_role, User.Role.ORGANIZER)
        self.client.post(url, {"platform_role": "organizer"})
        self.member.refresh_from_db()
        self.assertFalse(self.member.is_active)

    def test_admin_cannot_lock_themselves_out(self):
        self.client.post(reverse("accounts:admin_user_update", args=[self.admin.pk]),
                         {"platform_role": "member", "is_active": "on"})
        self.admin.refresh_from_db()
        self.assertEqual(self.admin.platform_role, User.Role.ADMIN)
