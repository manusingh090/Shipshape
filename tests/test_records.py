"""Certificates and verifiable records."""

import json
import os
import re
import subprocess
import sys
from datetime import timedelta
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.test import Client, SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from events.models import Award, EventRole, Prize
from judging.models import Assignment, Criterion
from judging.scoring import save_score
from projects.models import Project
from records import ed25519, pdf, services, signing
from records.models import Record

from .factories import add_staff, make_event, make_team, make_user
from .test_api import ApiClient

VERIFY_SCRIPT = Path(settings.BASE_DIR) / "records" / "verify.py"


class Ed25519Tests(SimpleTestCase):
    """RFC 8032, section 7.1, tests 1 and 2."""

    VECTORS = [
        ("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60",
         "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a", "",
         "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"),
        ("4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb",
         "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c", "72",
         "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00"),
    ]

    def test_the_rfc_vectors(self):
        for secret, public, message, signature in self.VECTORS:
            secret, public, message, signature = map(bytes.fromhex, (secret, public, message, signature))
            self.assertEqual(ed25519.public_key(secret), public)
            self.assertEqual(ed25519.sign(secret, message), signature)
            self.assertTrue(ed25519.verify(public, message, signature))

    def test_any_change_breaks_the_signature(self):
        secret = bytes(range(32))
        public = ed25519.public_key(secret)
        signature = ed25519.sign(secret, b"certificate")
        self.assertFalse(ed25519.verify(public, b"certificatE", signature))
        self.assertFalse(ed25519.verify(public, b"certificate", signature[:-1] + bytes([signature[-1] ^ 1])))
        self.assertFalse(ed25519.verify(ed25519.public_key(bytes(32)), b"certificate", signature))


class PdfTests(SimpleTestCase):
    def test_a_valid_single_page_pdf_with_its_text(self):
        page = pdf.Page()
        page.text(421, 300, "Zoë (Brontë) \\ 李", font="serif-bold", size=30, align="center")
        data = pdf.document(page, title="Test")
        self.assertTrue(data.startswith(b"%PDF-1.4"))
        self.assertTrue(data.rstrip().endswith(b"%%EOF"))
        # The cross-reference table points at each object exactly.
        xref_at = int(re.search(rb"startxref\n(\d+)", data).group(1))
        self.assertTrue(data[xref_at:].startswith(b"xref"))
        offsets = [int(o) for o in re.findall(rb"(\d{10}) 00000 n", data)]
        for number, offset in enumerate(offsets, start=1):
            self.assertTrue(data[offset:].startswith(f"{number} 0 obj".encode()), number)
        self.assertIn(b"/BaseFont /Times-Bold", data)

    def test_text_is_measured_with_the_fonts_widths(self):
        self.assertAlmostEqual(pdf.width("M", "serif", 10), 8.89)
        self.assertAlmostEqual(pdf.width("M", "mono", 10), 6.0)
        self.assertEqual(pdf.width("é", "serif", 10), pdf.width("e", "serif", 10))
        self.assertLessEqual(max(pdf.width(l, "serif", 15) for l in pdf.wrap("word " * 80, "serif", 15, 300)), 300)


class Setup(TestCase):
    def setUp(self):
        self.event = make_event(slug="certified", phase="closed")
        self.track = self.event.tracks.get()
        self.organizer = make_user("org@example.org", name="Rosa Mendel", role=User.Role.ORGANIZER)
        add_staff(self.event, self.organizer)
        self.priya, self.sam = make_user("priya@example.org", name="Priya"), make_user("sam@example.org", name="Sam")
        self.team = make_team(self.event, self.priya, self.sam, name="NorthKiln")
        self.project = self.make_project("Glass Signal", self.team)
        self.other = self.make_project("Quiet Hours", make_team(self.event, make_user("q@example.org"), name="Owls"))
        self.draft_team = make_team(self.event, make_user("draft@example.org"), name="Late")
        Project.objects.create(event=self.event, team=self.draft_team, title="Unfinished", status="draft")
        self.prize = Prize.objects.create(event=self.event, name="Best overall", value="$1,500", quantity=1)

    def make_project(self, title, team, track=None):
        return Project.objects.create(event=self.event, team=team, track=track or self.track, title=title, tagline="x",
                                      status="submitted", submitted_at=self.event.submissions_close_at - timedelta(hours=1))

    def judge(self, email="judge@example.org", marks=4):
        user = make_user(email, name="Diego")
        role = EventRole.objects.create(event=self.event, user=user, role=EventRole.Role.JUDGE)
        role.tracks.set([self.track])
        criterion, _ = Criterion.objects.get_or_create(event=self.event, key="impact", defaults={"label": "Impact"})
        Assignment.objects.create(event=self.event, judge=user, project=self.project)
        save_score(user, self.event, self.project.pk, {criterion.pk: marks}, "JUDGE NOTES", submit=True)
        return user


class AwardTests(Setup):
    def test_awards_follow_the_prize_rules(self):
        award = services.give_award(self.organizer, self.event, self.prize.pk, self.project.pk, "Quietly brilliant")
        with self.assertRaisesRegex(services.RecordError, "that many have it"):
            services.give_award(self.organizer, self.event, self.prize.pk, self.other.pk)
        track_prize = Prize.objects.create(event=self.event, name="Best tool", track=self.track, quantity=2)
        from events.models import Track
        elsewhere = self.make_project("Elsewhere", make_team(self.event, make_user("e@example.org"), name="E"),
                                      track=Track.objects.create(event=self.event, name="Play", position=2))
        with self.assertRaisesRegex(services.RecordError, "track"):
            services.give_award(self.organizer, self.event, track_prize.pk, elsewhere.pk)
        draft = Project.objects.get(title="Unfinished")
        with self.assertRaises(services.RecordError):
            services.give_award(self.organizer, self.event, track_prize.pk, draft.pk)
        self.assertEqual(award.note, "Quietly brilliant")

    def test_no_awards_before_the_deadline(self):
        open_event = make_event(slug="still-open", phase="open")
        prize = Prize.objects.create(event=open_event, name="Early", quantity=1)
        p = Project.objects.create(event=open_event, team=make_team(open_event, make_user("o@example.org")),
                                   title="P", status="submitted", submitted_at=timezone.now())
        with self.assertRaisesRegex(services.RecordError, "once submissions close"):
            services.give_award(self.organizer, open_event, prize.pk, p.pk)

    def test_winners_are_private_until_the_results_date(self):
        self.event.results_at = timezone.now() + timedelta(days=1)
        self.event.save()
        services.give_award(self.organizer, self.event, self.prize.pk, self.project.pk)
        page = Client().get(self.event.get_absolute_url())
        self.assertNotContains(page, "&#9733;")
        self.assertEqual(ApiClient().get("events/certified/awards").json()["results"], [])
        self.assertEqual(len(ApiClient(self.organizer).get("events/certified/awards").json()["results"]), 1)
        self.event.results_at = timezone.now() - timedelta(minutes=1)
        self.event.save()
        self.assertContains(Client().get(self.event.get_absolute_url()), "Glass Signal")


class IssueTests(Setup):
    def test_who_gets_what(self):
        services.give_award(self.organizer, self.event, self.prize.pk, self.project.pk, "Quietly brilliant")
        self.judge()
        made = services.issue(self.organizer, self.event, list(Record.Kind.values))
        self.assertEqual(made, {"participant": 3, "award": 2, "judge": 1, "organizer": 1})
        self.assertFalse(Record.objects.filter(user__email="draft@example.org").exists())  # no listed project
        award = Record.objects.get(kind="award", user=self.priya)
        payload = json.loads(award.payload)
        self.assertIn("won Best overall ($1,500) at Certified with the team NorthKiln", payload["statement"])
        self.assertEqual(services.status(award)["valid"], True)
        # Again: nothing new.
        self.assertEqual(sum(services.issue(self.organizer, self.event, list(Record.Kind.values)).values()), 0)

    def test_a_judges_record_commits_to_the_scores_without_showing_them(self):
        judge = self.judge()
        services.issue(self.organizer, self.event, ["judge"])
        record = Record.objects.get(kind="judge")
        public = record.payload + json.dumps(services.document(record))
        self.assertNotIn("JUDGE NOTES", public)
        self.assertNotIn('"impact"', public)  # not even the marks' keys
        page = Client().get(reverse("records:detail", args=[record.code]))
        self.assertContains(page, "submitting 1 review")
        self.assertNotContains(page, "The scores behind")  # a stranger isn't offered the receipt
        receipt_url = reverse("records:scores", args=[record.code])
        self.assertEqual(Client().get(receipt_url).status_code, 404)
        other_judge = make_user("other.judge@example.org")
        EventRole.objects.create(event=self.event, user=other_judge, role=EventRole.Role.JUDGE)
        stranger = Client()
        stranger.force_login(other_judge)
        self.assertEqual(stranger.get(receipt_url).status_code, 404)
        mine = Client()
        mine.force_login(judge)
        self.assertContains(mine.get(receipt_url), "It matches.")
        # Revising the score afterwards shows up as a change, not a mismatch.
        criterion = Criterion.objects.get(key="impact")
        save_score(judge, self.event, self.project.pk, {criterion.pk: 2}, "", submit=True)
        receipt = services.receipt(record)
        self.assertTrue(receipt["matches"])
        self.assertTrue(receipt["changed_since"])

    def test_revoking_is_visible_to_anyone_who_checks(self):
        services.issue(self.organizer, self.event, ["participant"])
        record = Record.objects.get(user=self.priya)
        with self.assertRaisesRegex(services.RecordError, "Say why"):
            services.revoke(self.organizer, record, "")
        services.revoke(self.organizer, record, "issued to the wrong team")
        page = Client().get(reverse("records:detail", args=[record.code]))
        self.assertContains(page, "issued to the wrong team")
        self.assertContains(page, "is-revoked")
        check = ApiClient().post("records/verify", services.document(record)).json()
        self.assertEqual((check["genuine"], check["revoked"], check["valid"]), (True, True, False))
        # A revoked record can be issued again, as a new one.
        self.assertEqual(services.issue(self.organizer, self.event, ["participant"])["participant"], 1)

    def test_taking_a_prize_back_needs_its_certificates_revoked_first(self):
        award = services.give_award(self.organizer, self.event, self.prize.pk, self.project.pk)
        services.issue(self.organizer, self.event, ["award"])
        with self.assertRaisesRegex(services.RecordError, "Revoke them first"):
            services.take_award(self.organizer, self.event, award.pk)
        for r in Record.objects.filter(kind="award"):
            services.revoke(self.organizer, r, "prize taken back")
        services.take_award(self.organizer, self.event, award.pk)
        self.assertFalse(Award.objects.exists())

    def test_only_organizers_issue(self):
        page = reverse("records:manage", args=[self.event.slug])
        stranger = Client()
        stranger.force_login(self.priya)
        self.assertEqual(stranger.get(page).status_code, 403)
        self.assertEqual(stranger.post(page, {"op": "issue", "kinds": ["participant"]}).status_code, 403)
        self.assertEqual(ApiClient(self.priya).post("events/certified/records", {"kinds": ["participant"]}).status_code, 403)
        org = Client()
        org.force_login(self.organizer)
        org.post(page, {"op": "issue", "kinds": ["participant", "organizer"]})
        self.assertEqual(Record.objects.count(), 4)
        mine = Client()
        mine.force_login(self.priya)
        self.assertContains(mine.get(reverse("accounts:account")), "Certificate of participation")


class VerificationTests(Setup):
    def setUp(self):
        super().setUp()
        services.issue(self.organizer, self.event, ["participant"])
        self.record = Record.objects.get(user=self.priya)
        self.document = json.loads(Client().get(reverse("records:json", args=[self.record.code])).content)
        self.tmp = Path(settings.DATA_DIR) / "test-record.json"

    def tearDown(self):
        self.tmp.unlink(missing_ok=True)

    def offline(self, document, *args):
        self.tmp.write_text(json.dumps(document), encoding="utf-8")
        return subprocess.run([sys.executable, str(VERIFY_SCRIPT), str(self.tmp), *args], capture_output=True, text=True)

    def test_the_downloaded_file_verifies_offline_without_django(self):
        key = signing.public_key_hex()
        good = self.offline(self.document, "--key", key)
        self.assertEqual(good.returncode, 0, good.stdout + good.stderr)
        self.assertIn("Genuine: Priya", good.stdout)
        tampered = json.loads(json.dumps(self.document))
        tampered["record"]["recipient"] = "Someone Else"
        self.assertEqual(self.offline(tampered, "--key", key).returncode, 1)
        forged_key = ed25519.public_key(bytes(32)).hex()
        self.assertEqual(self.offline(self.document, "--key", forged_key).returncode, 1)

    def test_the_verify_page_by_code_and_by_file(self):
        client = Client()
        self.assertRedirects(client.post(reverse("records:verify"), {"code": self.record.code.lower()}),
                             reverse("records:detail", args=[self.record.code]))
        self.assertContains(client.post(reverse("records:verify"), {"code": "ZZZZ-ZZZZ-ZZZZ-ZZZZ"}), "No record has that code")
        ok = client.post(reverse("records:verify"), {"document": json.dumps(self.document)})
        self.assertContains(ok, "Genuine.")
        self.document["record"]["statement"] += " And won everything."
        self.assertContains(client.post(reverse("records:verify"), {"document": json.dumps(self.document)}),
                            "the record was changed after signing")

    def test_the_pdf_is_the_signed_record(self):
        response = Client().get(reverse("records:pdf", args=[self.record.code]))
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertTrue(response.content.startswith(b"%PDF"))
        import zlib
        stream = zlib.decompress(re.search(rb"stream\n(.*?)\nendstream", response.content, re.S).group(1))
        self.assertIn(b"(Priya)", stream)
        self.assertIn(self.record.code.encode(), stream)

    def test_a_pinned_key_is_used_and_published(self):
        pinned = "11" * 32
        with mock.patch.dict(os.environ, {"RECORD_SIGNING_KEY": pinned}), mock.patch.dict(signing._cache, clear=True):
            self.assertEqual(signing.public_key_hex(), ed25519.public_key(bytes.fromhex(pinned)).hex())
            self.assertEqual(ApiClient().get("records/key").json()["public_key"], signing.public_key_hex())
