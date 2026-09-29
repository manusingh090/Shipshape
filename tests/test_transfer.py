"""Bulk import and export: an event goes out whole and comes back the same,
and spreadsheets of people follow the live event's rules."""

import io
import json
import zipfile
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from events.models import Award, CustomQuestion, Event, EventRole, Prize
from events.seeding import Seeder, load_fixture
from judging.results import compute_results
from projects.images import process_upload
from projects.models import Answer, Comment, Project, ProjectImage, Tag
from teams.models import Membership, Team
from transfer import csvimport, export
from transfer.importer import Importer, TransferError, read
from voting.models import Ballot, BallotEntry, VotingConfig

from .factories import add_staff, make_event, make_team, make_user, png_upload
from .test_api import ApiClient

FIXTURE = Path(settings.BASE_DIR) / "fixtures.json"


def ranking(event):
    return [(r.project.title, r.rank) for r in compute_results(event).rows]


def as_json(data):
    return json.dumps(data).encode("utf-8")


class FixtureRoundTripTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.original = load_fixture(FIXTURE)
        Seeder(demo=False).run(cls.original)
        cls.event = Event.objects.get()
        cls.organizer = make_user("org@example.org", role=User.Role.ORGANIZER)

    def test_the_export_is_the_fixture_it_came_from(self):
        out = export.fixture(self.event)
        want = json.loads(FIXTURE.read_text(encoding="utf-8"))
        for key in ("event", "tracks", "teams", "projects", "scores"):
            self.assertEqual(out[key], want[key], key)
        # Judges' tracks are a set; their order in the file isn't meaningful.
        norm = lambda judges: {j["id"]: {**j, "tracks": sorted(j["tracks"])} for j in judges}  # noqa: E731
        self.assertEqual(norm(out["judges"]), norm(want["judges"]))

    def test_importing_the_export_gives_the_same_results(self):
        report = Importer(self.organizer, "fixture", export.fixture(self.event), name="Copy").run()
        copy = Event.objects.get(slug=report["event"]["slug"])
        self.assertFalse(copy.is_published)
        self.assertTrue(EventRole.objects.filter(event=copy, user=self.organizer, role="organizer").exists())
        self.assertEqual(report["counts"]["projects"], 41)
        self.assertEqual(report["counts"]["scores"], 126)
        self.assertEqual(ranking(copy), ranking(self.event))

    def test_a_preview_saves_nothing(self):
        before = (Event.objects.count(), User.objects.count(), Project.objects.count())
        kind, data, media = read("fixtures.json", FIXTURE.read_bytes())
        report = Importer(self.organizer, kind, data, media, name="Copy").run(preview=True)
        self.assertEqual(report["counts"]["teams"], 40)
        self.assertEqual((Event.objects.count(), User.objects.count(), Project.objects.count()), before)

    def test_the_duplicate_stays_flagged(self):
        report = Importer(self.organizer, "fixture", export.fixture(self.event), name="Copy").run()
        copy = Event.objects.get(slug=report["event"]["slug"])
        self.assertEqual(Project.objects.filter(event=copy, duplicate_of__isnull=False).count(), 1)


class ArchiveRoundTripTests(TestCase):
    def setUp(self):
        self.organizer = make_user("org@example.org", role=User.Role.ORGANIZER)
        self.event = make_event(slug="rich", phase="closed", tagline="Build things", description="All of it.")
        add_staff(self.event, self.organizer)
        track = self.event.tracks.get()
        question = CustomQuestion.objects.create(event=self.event, prompt="Which APIs?", kind="short", position=1)
        captain, fan = make_user("cap@example.org"), make_user("fan@example.org")
        team = make_team(self.event, captain, make_user("mate@example.org"), name="Kestrel")
        path, w, h = process_upload(png_upload(), f"projects/{team.pk}")
        self.project = Project.objects.create(
            event=self.event, team=team, track=track, title="Tidewatch", tagline="Tides, watched", status="submitted",
            submitted_at=self.event.submissions_close_at - timedelta(hours=2), thumbnail=path)
        ProjectImage.objects.create(project=self.project, path=path, width=w, height=h)
        self.project.tags.add(Tag.objects.create(name="maps"))
        Answer.objects.create(project=self.project, question=question, value="OpenStreetMap")
        prize = Prize.objects.create(event=self.event, name="Best in show", value="A trophy")
        Award.objects.create(prize=prize, project=self.project, awarded_by=self.organizer)
        Comment.objects.create(project=self.project, author=fan, body="Lovely tide charts.")
        VotingConfig.objects.create(event=self.event, is_enabled=True, access="account", method="single", max_votes=1)
        ballot = Ballot.objects.create(event=self.event, kind="account", user=fan)
        BallotEntry.objects.create(ballot=ballot, project=self.project, votes=1)

    def round_trip(self):
        raw = export.zip_bytes(self.event)
        kind, data, media = read("rich-archive.zip", raw)
        self.assertEqual(kind, "archive")
        report = Importer(self.organizer, kind, data, media, name="Rich again").run()
        return Event.objects.get(slug=report["event"]["slug"]), report

    def test_everything_comes_back(self):
        copy, report = self.round_trip()
        self.assertEqual(copy.tagline, "Build things")
        project = Project.objects.get(event=copy)
        self.assertEqual(project.title, "Tidewatch")
        self.assertEqual(list(project.tags.values_list("name", flat=True)), ["maps"])
        self.assertEqual(project.answers.get().value, "OpenStreetMap")
        self.assertTrue(project.thumbnail)
        self.assertEqual(project.images.count(), 1)
        self.assertEqual(set(project.team.memberships.values_list("user__email", flat=True)),
                         {"cap@example.org", "mate@example.org"})
        self.assertEqual(Award.objects.get(project=project).prize.name, "Best in show")
        self.assertEqual(Comment.objects.get(project=project).body, "Lovely tide charts.")
        self.assertEqual(BallotEntry.objects.get(project=project).votes, 1)
        self.assertEqual(copy.voting_config.method, "single")
        self.assertEqual(report["counts"]["images"], 2)

    def test_the_archive_holds_no_secrets(self):
        z = zipfile.ZipFile(io.BytesIO(export.zip_bytes(self.event)))
        names = set(z.namelist())
        for expected in ("README.txt", "fixtures.json", "shipshape.json", "csv/teams.csv"):
            self.assertIn(expected, names)
        text = z.read("shipshape.json").decode("utf-8")
        for secret in ("pbkdf2", "argon2", "password", "token_hash", User.objects.get(email="cap@example.org").password):
            self.assertNotIn(secret, text)

    def test_people_are_matched_by_email_and_new_ones_cant_sign_in_yet(self):
        data = export.archive(self.event)
        User.objects.filter(email="fan@example.org").delete()
        Importer(self.organizer, "archive", data, name="Again").run()
        self.assertEqual(User.objects.filter(email="cap@example.org").count(), 1)
        self.assertFalse(User.objects.get(email="fan@example.org").has_usable_password())


class BadFileTests(TestCase):
    def test_things_that_arent_events_are_refused(self):
        for name, raw in [("x.json", b"not json"), ("x.json", b"[1, 2]"), ("x.json", as_json({"hello": 1})),
                          ("x.zip", b"PK\x03\x04broken"), ("x.json", as_json({"format": "shipshape-archive", "version": 9}))]:
            with self.assertRaises(TransferError, msg=raw[:20]):
                read(name, raw)

    def test_a_zip_bomb_is_refused_before_unpacking(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("fixtures.json", b"{}")
            z.writestr("media/big.bin", b"\0" * (201 * 1024 * 1024))
        with self.assertRaises(TransferError) as err:
            read("bomb.zip", buf.getvalue())
        self.assertIn("too big", err.exception.message)

    def test_paths_that_climb_out_are_ignored(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("fixtures.json", FIXTURE.read_bytes())
            z.writestr("media/../../evil.png", b"x")
        _, _, media = read("x.zip", buf.getvalue())
        self.assertEqual(media, {})


class CsvImportTests(TestCase):
    def setUp(self):
        self.organizer = make_user("org@example.org", role=User.Role.ORGANIZER)
        self.event = make_event(max_team_size=2)
        add_staff(self.event, self.organizer)

    def test_teams_go_in_and_rule_breakers_are_listed(self):
        judge = make_user("judge@example.org")
        add_staff(self.event, judge, role=EventRole.Role.JUDGE)
        sheet = ("team,member_email,member_name\n"
                 "Otters,a@example.org,Asha\nOtters,b@example.org,\nOtters,c@example.org,\n"
                 "Voles,judge@example.org,\nVoles,a@example.org,\nVoles,not-an-email,\n")
        report = csvimport.import_teams(self.organizer, self.event, sheet)
        self.assertEqual(report["added"], 2)
        self.assertEqual(report["skipped"], 4)
        otters = Team.objects.get(event=self.event, name="Otters")
        self.assertEqual(otters.memberships.get(role=Membership.Role.CAPTAIN).user.email, "a@example.org")
        joined = "\n".join(report["problems"])
        for reason in ("is full", "staff", "already on a team", "isn't an email"):
            self.assertIn(reason, joined)

    def test_a_preview_adds_nobody(self):
        report = csvimport.import_teams(self.organizer, self.event, "team,email\nOtters,a@example.org\n", preview=True)
        self.assertEqual(report["added"], 1)
        self.assertFalse(Team.objects.filter(event=self.event).exists())
        self.assertFalse(User.objects.filter(email="a@example.org").exists())

    def test_rosters_are_locked_after_the_deadline(self):
        Event.objects.filter(pk=self.event.pk).update(submissions_close_at=timezone.now() - timedelta(minutes=1))
        from events.deadline import WindowError
        with self.assertRaises(WindowError):
            csvimport.import_teams(self.organizer, self.event, "team,email\nOtters,a@example.org\n")

    def test_judges_need_known_tracks_and_organizers_cant_judge(self):
        sheet = "email,tracks\nlee@example.org,Tools\nkim@example.org,Underwater Basketry\norg@example.org,\n"
        report = csvimport.import_judges(self.organizer, self.event, sheet)
        self.assertEqual(report["added"], 1)
        self.assertTrue(EventRole.objects.filter(event=self.event, user__email="lee@example.org", role="judge").exists())
        self.assertIn("Underwater Basketry", " ".join(report["problems"]))
        self.assertEqual(report["skipped"], 2)

    def test_a_sheet_without_the_columns_is_refused(self):
        with self.assertRaises(TransferError):
            csvimport.import_teams(self.organizer, self.event, "name,phone\nx,1\n")


class PageTests(TestCase):
    def setUp(self):
        self.organizer = make_user("org@example.org", role=User.Role.ORGANIZER)
        self.event = make_event()
        add_staff(self.event, self.organizer)
        self.client = Client()
        self.client.force_login(self.organizer)

    def test_only_organizers_see_the_tab_and_downloads(self):
        outsider = Client()
        outsider.force_login(make_user("someone@example.org"))
        for name in ("transfer:manage", "transfer:archive", "transfer:fixture"):
            url = reverse(name, args=[self.event.slug])
            self.assertEqual(self.client.get(url).status_code, 200, name)
            self.assertIn(outsider.get(url).status_code, (403, 404), name)
        self.assertEqual(self.client.get(reverse("transfer:archive", args=[self.event.slug]))["Content-Type"],
                         "application/zip")

    def test_only_event_creators_can_import_an_event(self):
        member = Client()
        member.force_login(make_user("someone@example.org"))
        self.assertNotEqual(member.get(reverse("transfer:import")).status_code, 200)
        self.assertEqual(self.client.get(reverse("transfer:import")).status_code, 200)

    def test_preview_then_commit_through_the_page(self):
        url = reverse("transfer:import")
        upload = SimpleUploadedFile("fixtures.json", FIXTURE.read_bytes(), content_type="application/json")
        page = self.client.post(url, {"op": "preview", "file": upload, "name": "Sample again"})
        self.assertContains(page, "What will happen")
        self.assertEqual(Event.objects.count(), 1)
        done = self.client.post(url, {"op": "commit"})
        copy = Event.objects.get(name="Sample again")
        self.assertRedirects(done, reverse("events:manage", args=[copy.slug]))
        self.assertEqual(copy.projects.count(), 41)

    def test_csv_preview_then_apply_through_the_tab(self):
        url = reverse("transfer:manage", args=[self.event.slug])
        upload = SimpleUploadedFile("teams.csv", b"team,member_email\nOtters,a@example.org\n", content_type="text/csv")
        self.assertContains(self.client.post(url, {"op": "preview_teams", "file": upload}), "Would be added")
        self.assertFalse(Team.objects.filter(event=self.event).exists())
        self.client.post(url, {"op": "apply"})
        self.assertTrue(Team.objects.filter(event=self.event, name="Otters").exists())


class ApiTests(TestCase):
    def setUp(self):
        self.organizer = make_user("org@example.org", role=User.Role.ORGANIZER)
        self.event = make_event()
        add_staff(self.event, self.organizer)
        self.api = ApiClient(self.organizer)

    def test_export_and_import_by_api(self):
        fixture = self.api.get(f"events/{self.event.slug}/export/fixtures.json")
        self.assertEqual(fixture.status_code, 200)
        data = json.loads(fixture.content)
        preview = self.api.post("imports", {"data": data, "name": "Twin", "preview": True})
        self.assertEqual(preview.status_code, 200, preview.content)
        self.assertTrue(preview.json()["preview"])
        self.assertEqual(Event.objects.count(), 1)
        made = self.api.post("imports", {"data": data, "name": "Twin"})
        self.assertEqual(made.status_code, 200)
        self.assertTrue(Event.objects.filter(name="Twin").exists())

    def test_members_cant_import_and_bad_files_say_why(self):
        member = ApiClient(make_user("m@example.org"))
        self.assertEqual(member.post("imports", {"data": {}}).status_code, 403)
        refused = self.api.post("imports", {"data": {"hello": 1}})
        self.assertEqual(refused.status_code, 400)
        self.assertEqual(refused.json()["error"], "import_refused")

    def test_csv_by_api(self):
        r = self.api.post(f"events/{self.event.slug}/import/judges", {"csv": "email\nlee@example.org\n"})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["added"], 1)
