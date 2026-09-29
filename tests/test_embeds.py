"""The embeddable gallery, and finding the key that signs judges' records."""

from datetime import timedelta

from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from embeds.models import EmbedSettings
from events.models import EventRole, Prize
from judging.models import Assignment, Criterion
from judging.scoring import save_score
from projects.models import Project
from records import services as records, signing

from .factories import add_staff, make_event, make_team, make_user
from .test_api import ApiClient


class Setup(TestCase):
    def setUp(self):
        self.event = make_event(slug="shown", phase="closed")
        self.track = self.event.tracks.get()
        self.organizer = make_user("org@example.org", role=User.Role.ORGANIZER)
        add_staff(self.event, self.organizer)
        self.listed = [self.project(f"Listed {n}") for n in range(5)]
        self.draft = Project.objects.create(event=self.event, team=make_team(self.event, make_user("d@example.org"), name="D"),
                                            title="SECRET DRAFT", status="draft")

    def project(self, title):
        team = make_team(self.event, make_user(f"{title.split()[-1]}@example.org"), name=f"Team {title}")
        return Project.objects.create(event=self.event, team=team, track=self.track, title=title, tagline="x",
                                      status="submitted", submitted_at=self.event.submissions_close_at - timedelta(hours=1))

    def widget(self, client=None, **params):
        return (client or Client()).get(reverse("embeds:widget", args=[self.event.slug]), params)


class WidgetTests(Setup):
    def test_it_shows_the_public_gallery_even_to_a_signed_in_organizer(self):
        organizer = Client()
        organizer.force_login(self.organizer)
        self.assertContains(organizer.get(reverse("projects:detail", args=[self.draft.pk])),
                            "SECRET DRAFT")  # the organizer can see the draft on the portal...
        page = self.widget(organizer)
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, "SECRET DRAFT")  # ...but never inside someone else's page
        for p in self.listed:
            self.assertContains(page, p.title)
        self.assertContains(page, '<base target="_blank">')

    def test_only_the_widget_can_be_framed_and_only_where_allowed(self):
        page = self.widget()
        self.assertIn("frame-ancestors 'self' *", page["Content-Security-Policy"])
        self.assertNotIn("X-Frame-Options", page)
        normal = Client().get(self.event.get_absolute_url())
        self.assertIn("frame-ancestors 'none'", normal["Content-Security-Policy"])
        self.assertEqual(normal["X-Frame-Options"], "DENY")
        EmbedSettings.objects.create(event=self.event, allowed_origins="https://hack.example.org\nhttps://sponsor.example.com")
        self.assertIn("frame-ancestors 'self' https://hack.example.org https://sponsor.example.com",
                      self.widget()["Content-Security-Policy"])

    def test_switched_off_or_unpublished_means_nothing_to_embed(self):
        EmbedSettings.objects.create(event=self.event, enabled=False)
        self.assertEqual(self.widget().status_code, 404)
        self.assertEqual(Client().get(reverse("embeds:feed", args=[self.event.slug])).status_code, 404)
        EmbedSettings.objects.filter(event=self.event).update(enabled=True)
        self.event.is_published = False
        self.event.save()
        self.assertEqual(self.widget().status_code, 404)

    def test_options(self):
        page = self.widget(sort="title", limit="2")
        self.assertContains(page, "Listed 0")
        self.assertContains(page, "Listed 1")
        self.assertNotContains(page, "Listed 4")
        self.assertContains(self.widget(limit="9999"), "Listed 4")  # clamped, not an error
        self.assertContains(self.widget(theme="dark"), 'class="embed theme-dark"')
        self.assertContains(self.widget(theme="<script>"), 'class="embed theme-auto"')
        shuffled = self.widget(limit="3").content.decode()
        self.assertEqual(sum(p.title in shuffled for p in self.listed), 3)

    def test_winners_appear_once_awards_are_public(self):
        self.event.results_at = timezone.now() + timedelta(days=1)
        self.event.save()
        prize = Prize.objects.create(event=self.event, name="Best overall", quantity=1)
        records.give_award(self.organizer, self.event, prize.pk, self.listed[0].pk)
        self.assertNotContains(self.widget(), "Best overall")
        self.event.results_at = timezone.now() - timedelta(minutes=1)
        self.event.save()
        self.assertContains(self.widget(), "&#9733; Best overall")

    def test_the_feed_and_loader_are_readable_from_other_sites(self):
        feed = Client().get(reverse("embeds:feed", args=[self.event.slug]), {"limit": 48})
        self.assertEqual(feed["Access-Control-Allow-Origin"], "*")
        titles = [p["title"] for p in feed.json()["projects"]]
        self.assertEqual(sorted(titles), sorted(p.title for p in self.listed))
        loader = Client().get(reverse("embeds:loader"))
        self.assertTrue(loader["Content-Type"].startswith("text/javascript"))
        self.assertIn(b"data-shipshape-gallery", loader.content)


class ManageTests(Setup):
    def test_organizers_get_snippets_and_the_settings(self):
        url = reverse("embeds:manage", args=[self.event.slug])
        outsider = Client()
        outsider.force_login(make_user("x@example.org"))
        self.assertEqual(outsider.get(url).status_code, 403)
        org = Client()
        org.force_login(self.organizer)
        page = org.get(url, {"track": self.track.pk, "sort": "title"})
        script = page.context["snippets"]["script"]
        self.assertIn('data-shipshape-gallery="shown" data-track="%d" data-sort="title"' % self.track.pk, script)
        self.assertIn("/embed.js", script)
        bad = org.post(url, {"enabled": "on", "allowed_origins": "https://ok.example.org/some/path"})
        self.assertContains(bad, "isn&#x27;t a site address")
        org.post(url, {"enabled": "on", "allowed_origins": "https://ok.example.org/"})
        self.assertEqual(EmbedSettings.objects.get().origin_list, ["https://ok.example.org"])

    def test_the_api_too(self):
        api = ApiClient(self.organizer)
        self.assertIn("embed.js", api.get("events/shown/embed").json()["script"])
        r = api.patch("events/shown/embed", {"enabled": False})
        self.assertFalse(r.json()["enabled"])
        self.assertEqual(self.widget().status_code, 404)
        self.assertEqual(ApiClient(make_user("y@example.org")).get("events/shown/embed").status_code, 403)


class RecordDiscoveryTests(Setup):
    def test_the_key_is_published_where_programs_and_people_look(self):
        doc = Client().get("/.well-known/shipshape-records.json").json()
        self.assertEqual(doc["public_key"], signing.public_key_hex())
        self.assertEqual(doc["algorithm"], "Ed25519")
        self.assertNotContains(Client().get(self.event.get_absolute_url()), "Signed records")
        judge = make_user("judge@example.org", name="Diego")
        role = EventRole.objects.create(event=self.event, user=judge, role=EventRole.Role.JUDGE)
        role.tracks.set([self.track])
        criterion = Criterion.objects.create(event=self.event, key="impact", label="Impact")
        Assignment.objects.create(event=self.event, judge=judge, project=self.listed[0])
        save_score(judge, self.event, self.listed[0].pk, {criterion.pk: 4}, "", submit=True)
        records.issue(self.organizer, self.event, ["judge"])
        page = Client().get(self.event.get_absolute_url())
        self.assertContains(page, "Signed records")
        self.assertContains(page, doc["key_id"])
        mine = Client()
        mine.force_login(judge)
        code = judge.records.get().code
        self.assertContains(mine.get(reverse("judging:queue", args=[self.event.slug])), code)
