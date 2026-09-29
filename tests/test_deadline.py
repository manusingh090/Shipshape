"""The deadline has to hold on every path, not just in the UI."""

import json
import shutil
import tempfile
from datetime import timedelta

from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from events.models import Activity, Event
from projects.models import Project

from .factories import complete_submission, make_event, make_team, make_user, png_upload


class ClosedEventTests(TestCase):
    def setUp(self):
        self.event = make_event(phase="closed")
        self.priya = make_user("priya@example.org")
        self.team = make_team(self.event, self.priya)
        self.project = Project.objects.create(
            event=self.event, team=self.team, title="Handed In", tagline="Before the whistle",
            status=Project.Status.SUBMITTED, submitted_at=self.event.submissions_close_at - timedelta(hours=2),
        )
        self.client.force_login(self.priya)

    def test_api_post_is_refused_because_of_the_deadline(self):
        # enforce_csrf_checks proves the refusal is the deadline, not a missing token.
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.priya)
        response = client.post(
            reverse("projects:api_submission", args=[self.event.slug]),
            data=json.dumps({"title": "late probe", "summary": "probe"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"], "submissions_closed")
        self.project.refresh_from_db()
        self.assertEqual(self.project.title, "Handed In")
        self.assertTrue(Activity.objects.filter(event=self.event, verb="late.refused").exists())

    def test_web_form_post_is_refused(self):
        response = self.client.post(
            reverse("projects:edit", args=[self.event.slug]),
            complete_submission(self.event, title="Changed after the deadline"),
        )
        self.assertEqual(response.status_code, 403)
        self.assertContains(response, "closed", status_code=403)
        self.project.refresh_from_db()
        self.assertEqual(self.project.title, "Handed In")

    def test_the_form_renders_read_only(self):
        response = self.client.get(reverse("projects:edit", args=[self.event.slug]))
        self.assertContains(response, "<fieldset disabled>")
        self.assertNotContains(response, 'value="submit"')

    def test_withdraw_is_refused(self):
        self.client.post(reverse("projects:withdraw", args=[self.event.slug]))
        self.project.refresh_from_db()
        self.assertEqual(self.project.status, Project.Status.SUBMITTED)

    def test_image_upload_is_refused(self):
        media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, media, ignore_errors=True)
        with override_settings(MEDIA_ROOT=media):
            self.client.post(reverse("projects:image_upload", args=[self.event.slug]), {"images": png_upload()})
        self.assertEqual(self.project.images.count(), 0)

    def test_rosters_are_locked(self):
        tom = make_user("tom@example.org")
        self.client.force_login(tom)
        self.client.post(reverse("teams:join", args=[self.team.invite_code]))
        self.assertFalse(self.team.memberships.filter(user=tom).exists())
        self.client.post(reverse("teams:mine", args=[self.event.slug]), {"name": "Latecomers"})
        self.assertFalse(tom.memberships.exists())

        self.client.force_login(self.priya)
        self.client.post(reverse("teams:leave", args=[self.event.slug]))
        self.assertTrue(self.team.memberships.filter(user=self.priya).exists())

    def test_organizer_extension_reopens_immediately(self):
        Event.objects.filter(pk=self.event.pk).update(submissions_close_at=timezone.now() + timedelta(hours=1))
        response = self.client.post(
            reverse("projects:edit", args=[self.event.slug]),
            complete_submission(self.event, title="Extended edit", action="save"),
        )
        self.assertEqual(response.status_code, 302)
        self.project.refresh_from_db()
        self.assertEqual(self.project.title, "Extended edit")


class PhaseBoundaryTests(TestCase):
    def test_the_deadline_instant_counts_as_closed(self):
        event = make_event()
        self.assertEqual(event.phase(event.submissions_close_at), Event.Phase.CLOSED)
        self.assertEqual(event.phase(event.submissions_close_at - timedelta(microseconds=1)), Event.Phase.OPEN)
        self.assertEqual(event.phase(event.starts_at - timedelta(seconds=1)), Event.Phase.UPCOMING)


class UpcomingEventTests(TestCase):
    def setUp(self):
        self.event = make_event(phase="upcoming")
        self.priya = make_user("priya@example.org")
        self.client.force_login(self.priya)

    def test_teams_can_form_before_kickoff(self):
        self.client.post(reverse("teams:mine", args=[self.event.slug]), {"name": "Early Birds"})
        self.assertTrue(self.priya.memberships.filter(event=self.event).exists())

    def test_projects_cannot_be_saved_before_kickoff(self):
        make_team(self.event, self.priya)
        response = self.client.post(
            reverse("projects:api_submission", args=[self.event.slug]),
            data=json.dumps({"title": "Too early"}), content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"], "submissions_not_open")
        self.assertFalse(Project.objects.exists())
