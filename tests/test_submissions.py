import json
import shutil
import tempfile
from datetime import timedelta
from pathlib import Path

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from events.models import Event
from projects.forms import parse_tags
from projects.models import Project

from .factories import add_question, add_staff, complete_submission, make_event, make_team, make_user, png_upload


class SubmissionFlowTests(TestCase):
    def setUp(self):
        self.event = make_event()
        self.priya = make_user("priya@example.org")
        self.tom = make_user("tom@example.org")
        self.team = make_team(self.event, self.priya, self.tom)
        self.client.force_login(self.priya)
        self.edit_url = reverse("projects:edit", args=[self.event.slug])

    def save(self, action="save", **fields):
        data = {"title": "Quiet Hours", **fields, "action": action}
        return self.client.post(self.edit_url, data)

    def test_a_draft_needs_only_a_name_and_stays_private(self):
        self.assertEqual(self.save().status_code, 302)
        project = Project.objects.get()
        self.assertEqual(project.status, Project.Status.DRAFT)

        stranger = make_user("stranger@example.org")
        self.client.force_login(stranger)
        self.assertEqual(self.client.get(project.get_absolute_url()).status_code, 404)
        self.assertNotContains(self.client.get(reverse("projects:gallery")), "Quiet Hours")

        self.client.force_login(self.tom)
        self.assertEqual(self.client.get(project.get_absolute_url()).status_code, 200)
        organizer = make_user("org@example.org")
        add_staff(self.event, organizer)
        self.client.force_login(organizer)
        self.assertEqual(self.client.get(project.get_absolute_url()).status_code, 200)

    def test_submitting_an_incomplete_project_is_refused(self):
        response = self.save(action="submit")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(Project.objects.filter(status=Project.Status.SUBMITTED).count(), 0)

    def test_submitting_a_complete_project_puts_it_in_the_gallery(self):
        response = self.client.post(self.edit_url, {**complete_submission(self.event), "action": "submit"})
        self.assertEqual(response.status_code, 302)
        project = Project.objects.get()
        self.assertEqual(project.status, Project.Status.SUBMITTED)
        self.assertIsNotNone(project.submitted_at)
        self.assertEqual(sorted(project.tags.values_list("name", flat=True)), ["django", "sqlite"])
        self.client.logout()
        # Held back until the deadline, so a rival can't copy it while there's still time...
        self.assertNotContains(self.client.get(reverse("projects:gallery")), "Quiet Hours")
        self.assertEqual(self.client.get(project.get_absolute_url()).status_code, 404)
        # ...and public the moment submissions close.
        Event.objects.filter(pk=self.event.pk).update(submissions_close_at=timezone.now() - timedelta(seconds=1))
        self.assertContains(self.client.get(reverse("projects:gallery")), "Quiet Hours")

    def test_organizers_can_show_projects_before_the_deadline(self):
        Event.objects.filter(pk=self.event.pk).update(gallery_before_deadline=True)
        self.client.post(self.edit_url, {**complete_submission(self.event), "action": "submit"})
        self.client.logout()
        self.assertContains(self.client.get(reverse("projects:gallery")), "Quiet Hours")

    def test_teammates_can_keep_editing_until_the_deadline(self):
        self.client.post(self.edit_url, {**complete_submission(self.event), "action": "submit"})
        self.client.force_login(self.tom)
        self.client.post(self.edit_url, {**complete_submission(self.event, tagline="Now with timers"), "action": "save"})
        project = Project.objects.get()
        self.assertEqual(project.tagline, "Now with timers")
        self.assertEqual(project.status, Project.Status.SUBMITTED)
        self.assertEqual(project.last_edited_by, self.tom)

    def test_a_submitted_project_cannot_be_emptied(self):
        self.client.post(self.edit_url, {**complete_submission(self.event), "action": "submit"})
        response = self.client.post(self.edit_url, {**complete_submission(self.event, description=""), "action": "save"})
        self.assertEqual(response.status_code, 400)
        self.assertNotEqual(Project.objects.get().description, "")

    def test_required_organizer_questions_block_submission(self):
        question = add_question(self.event)
        response = self.client.post(self.edit_url, {**complete_submission(self.event), "action": "submit"})
        self.assertEqual(response.status_code, 400)
        data = {**complete_submission(self.event), "action": "submit", f"q_{question.pk}": "None, all our own."}
        self.assertEqual(self.client.post(self.edit_url, data).status_code, 302)
        project = Project.objects.get()
        self.assertEqual(project.answers.get().value, "None, all our own.")

    def test_private_answers_stay_private(self):
        Event.objects.filter(pk=self.event.pk).update(gallery_before_deadline=True)
        question = add_question(self.event, prompt="Shipping country", required=False, public=False, kind="short")
        data = {**complete_submission(self.event), "action": "submit", f"q_{question.pk}": "Portugal"}
        self.client.post(self.edit_url, data)
        project = Project.objects.get()
        self.assertContains(self.client.get(project.get_absolute_url()), "Portugal")
        self.client.logout()
        self.assertNotContains(self.client.get(project.get_absolute_url()), "Portugal")

    def test_withdraw_moves_back_to_draft(self):
        self.client.post(self.edit_url, {**complete_submission(self.event), "action": "submit"})
        self.client.post(reverse("projects:withdraw", args=[self.event.slug]))
        self.assertEqual(Project.objects.get().status, Project.Status.DRAFT)

    def test_organizers_and_judges_cannot_submit(self):
        judge = make_user("judge@example.org")
        add_staff(self.event, judge, "judge")
        self.client.force_login(judge)
        response = self.client.get(self.edit_url)
        self.assertRedirects(response, reverse("events:detail", args=[self.event.slug]))


class ApiTests(TestCase):
    def setUp(self):
        self.event = make_event()
        self.priya = make_user("priya@example.org")
        make_team(self.event, self.priya)
        self.client.force_login(self.priya)
        self.url = reverse("projects:api_submission", args=[self.event.slug])

    def post(self, body, **headers):
        return self.client.post(self.url, data=json.dumps(body), content_type="application/json", **headers)

    def test_create_submit_and_read_back(self):
        response = self.post({"title": "Quiet Hours"})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["status"], "draft")
        body = {
            "summary": "Mutes notifications.", "description": "Long form.",
            "track": self.event.tracks.first().pk, "tags": ["go", "htmx"], "action": "submit",
        }
        response = self.post(body)
        self.assertEqual(response.status_code, 200, response.content)
        data = self.client.get(self.url).json()
        self.assertEqual(data["status"], "submitted")
        self.assertEqual(data["tagline"], "Mutes notifications.")
        self.assertEqual(data["tags"], ["go", "htmx"])

    def test_unauthenticated_gets_401(self):
        self.client.logout()
        self.assertEqual(self.post({"title": "x"}).status_code, 401)

    def test_form_encoded_bodies_are_rejected(self):
        response = self.client.post(self.url, {"title": "x"})
        self.assertEqual(response.status_code, 415)

    def test_cross_origin_requests_are_rejected(self):
        response = self.post({"title": "x"}, HTTP_ORIGIN="https://evil.example")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"], "cross_origin")
        self.assertFalse(Project.objects.exists())

    def test_validation_errors_come_back_as_fields(self):
        response = self.post({"title": "x", "repo_url": "javascript:alert(1)"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("repo_url", response.json()["fields"])


class TagTests(TestCase):
    def test_tags_are_normalised(self):
        self.assertEqual(parse_tags(" Django,  raspberry   PI , django,C++ "), ["django", "raspberry pi", "c++"])

    def test_bad_tags_are_refused(self):
        from django import forms
        with self.assertRaises(forms.ValidationError):
            parse_tags("<script>")
        with self.assertRaises(forms.ValidationError):
            parse_tags(",".join(f"t{i}" for i in range(13)))


class ImageTests(TestCase):
    def setUp(self):
        self.media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.media, ignore_errors=True)
        override = override_settings(MEDIA_ROOT=self.media)
        override.enable()
        self.addCleanup(override.disable)
        self.event = make_event()
        self.priya = make_user("priya@example.org")
        make_team(self.event, self.priya)
        self.client.force_login(self.priya)
        self.client.post(reverse("projects:edit", args=[self.event.slug]), {"title": "Quiet Hours", "action": "save"})
        self.project = Project.objects.get()

    def test_images_are_re_encoded_and_served_only_to_allowed_viewers(self):
        self.client.post(reverse("projects:image_upload", args=[self.event.slug]), {"images": png_upload()})
        image = self.project.images.get()
        self.assertTrue(image.path.endswith(".jpg"))
        self.assertTrue((Path(self.media) / image.path).is_file())
        url = reverse("media", args=[image.path])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        # The project is still a draft, so a stranger is refused.
        self.client.logout()
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_non_images_are_refused(self):
        fake = SimpleUploadedFile("evil.png", b"<html><script>alert(1)</script></html>", content_type="image/png")
        self.client.post(reverse("projects:image_upload", args=[self.event.slug]), {"images": fake})
        self.assertEqual(self.project.images.count(), 0)

    def test_path_traversal_is_refused(self):
        self.assertEqual(self.client.get("/media/../settings.py").status_code, 404)
        self.assertEqual(self.client.get("/media/projects/../../secret_key").status_code, 404)
