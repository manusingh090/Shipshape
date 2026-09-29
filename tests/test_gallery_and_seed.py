import json
from pathlib import Path

from django.conf import settings
from django.contrib.sessions.models import Session
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from events.markdown import render
from events.models import Event
from events.seeding import Seeder, load_fixture, name_from_email
from projects.models import Project, Tag

FIXTURE = Path(settings.BASE_DIR) / "fixtures.json"


class SeedTests(TestCase):
    def setUp(self):
        self.fixture = load_fixture(FIXTURE)
        Seeder(demo=True, password="dogfood-demo").run(self.fixture)

    def test_everything_from_the_fixture_arrives_once(self):
        Seeder(demo=True, password="dogfood-demo").run(self.fixture)  # second boot
        event = Event.objects.get(external_id="evt_01")
        self.assertEqual(event.tracks.count(), len(self.fixture["tracks"]))
        self.assertEqual(event.teams.count(), len(self.fixture["teams"]))
        self.assertEqual(Project.objects.filter(event=event).count(), len(self.fixture["projects"]))
        self.assertEqual(event.roles.filter(role="judge").count(), len(self.fixture["judges"]))

    def test_the_fixture_event_is_closed_with_its_own_deadline(self):
        event = Event.objects.get(external_id="evt_01")
        self.assertEqual(event.submissions_close_at.isoformat(), "2026-03-01T18:00:00+00:00")
        self.assertTrue(event.is_closed)

    def test_the_duplicate_submission_is_flagged(self):
        duplicate = Project.objects.get(external_id="prj_41")
        self.assertEqual(duplicate.duplicate_of.external_id, "prj_07")

    def test_demo_cookies_authenticate(self):
        client = Client()
        client.cookies["session"] = "prt_91d7aa3f"
        response = client.get(reverse("accounts:account"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "priya1@example.org")

    def test_demo_mode_off_removes_the_fixed_sessions(self):
        Seeder(demo=False).run(self.fixture)
        self.assertFalse(Session.objects.filter(session_key="prt_91d7aa3f").exists())

    def test_member_names_are_readable(self):
        self.assertEqual(name_from_email("priya1@example.org"), "Priya")
        self.assertEqual(name_from_email("member7_2@example.org"), "Member 7-2")


class GalleryTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        Seeder(demo=False).run(load_fixture(FIXTURE))

    def test_gallery_is_public_and_shows_fixture_projects(self):
        response = self.client.get(reverse("projects:gallery"))
        self.assertEqual(response.status_code, 200)
        for title in ("Glass Signal", "Small Meadow", "Deep Compass"):
            self.assertContains(response, title)

    def test_duplicates_are_left_out(self):
        response = self.client.get(reverse("projects:gallery"))
        self.assertEqual(response.context["page"].paginator.count, 40)

    def test_search_matches_title_team_and_tags(self):
        project = Project.objects.get(external_id="prj_01")
        project.tags.add(Tag.objects.create(name="rust"))
        found = {p.title for p in self.client.get("/projects/?q=glass").context["page"].object_list}
        # Two titles contain "glass"; Warm Beacon comes in through its team, GlassDrift.
        self.assertEqual(found, {"Glass Signal", "Glass Beacon", "Warm Beacon"})
        self.assertEqual(self.client.get("/projects/?q=northkiln").context["page"].paginator.count, 1)
        self.assertEqual(self.client.get("/projects/?tag=rust").context["page"].paginator.count, 1)

    def test_track_filter(self):
        event = Event.objects.get(external_id="evt_01")
        track = event.tracks.get(external_id="trk_06")
        response = self.client.get(reverse("projects:event_gallery", args=[event.slug]), {"track": track.pk})
        self.assertEqual(response.context["page"].paginator.count, 3)

    def test_shuffle_is_stable_for_a_seed(self):
        first = [p.pk for p in self.client.get("/projects/?sort=shuffle&seed=7").context["page"].object_list]
        again = [p.pk for p in self.client.get("/projects/?sort=shuffle&seed=7").context["page"].object_list]
        other = [p.pk for p in self.client.get("/projects/?sort=shuffle&seed=8").context["page"].object_list]
        self.assertEqual(first, again)
        self.assertNotEqual(first, other)

    def test_public_api_lists_the_same_projects(self):
        data = json.loads(self.client.get("/api/projects?limit=200").content)
        self.assertEqual(data["count"], 40)


class MarkdownTests(TestCase):
    def test_html_is_escaped(self):
        html = render("<script>alert(1)</script> **bold**")
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("<strong>bold</strong>", html)

    def test_only_web_links_become_anchors(self):
        self.assertIn('href="https://example.org"', render("[site](https://example.org)"))
        self.assertNotIn("href", render("[x](javascript:alert(1))"))
        # Quotes inside a URL stay escaped, so they can't close the attribute.
        html = render('[x](https://a.b/"onerror="alert(1))')
        self.assertNotIn('"onerror', html)
        self.assertNotIn('onerror="', html)
        self.assertIn("&quot;onerror=&quot;", html)


@override_settings(ALLOWED_HOSTS=["testserver"])
class SecurityHeaderTests(TestCase):
    def test_csp_blocks_inline_and_third_party_content(self):
        response = self.client.get(reverse("home"))
        csp = response["Content-Security-Policy"]
        self.assertIn("default-src 'self'", csp)
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertEqual(response["X-Frame-Options"], "DENY")
