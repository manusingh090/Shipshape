"""Judging, through the same requests a browser or curl would make."""

import csv
import io
import json
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from events.models import Event, EventRole, Track
from events.seeding import Seeder, load_fixture
from judging import staff
from judging.assigning import assign_manually, plan_batch, run_batch, scoped_inputs
from judging.errors import JudgingError
from judging.models import Assignment, AssignmentBatch, Conflict, Criterion, JudgeInvite, Score, ScoreRevision
from judging.results import compute_progress, compute_results
from judging.scoring import save_score
from projects.models import Project

from .factories import add_staff, make_event, make_team, make_user


class JudgingSetup(TestCase):
    """A closed event with two tracks, three judges and three projects."""

    def setUp(self):
        self.event = make_event(slug="judged-hack", phase="closed")
        self.t1 = self.event.tracks.get()
        self.t2 = Track.objects.create(event=self.event, name="Climate", position=2)
        for position, key in enumerate(["functionality", "quality", "innovation"], start=1):
            Criterion.objects.create(event=self.event, key=key, label=key.title(), weight=1, position=position)
        self.organizer = make_user("org@example.org", role=User.Role.ORGANIZER)
        add_staff(self.event, self.organizer)
        self.ada = self.judge("ada@example.org", [self.t1], external_id="jdg_ada")
        self.bo = self.judge("bo@example.org", [self.t1], external_id="jdg_bo")
        self.cy = self.judge("cy@example.org", [self.t2], external_id="jdg_cy")
        self.p1 = self.project("Quiet Hours", self.t1)
        self.p2 = self.project("Loud Minutes", self.t1)
        self.p3 = self.project("Warm Rivers", self.t2)

    def judge(self, email, tracks, external_id=""):
        user = make_user(email)
        user.external_id = external_id
        user.save()
        role = EventRole.objects.create(event=self.event, user=user, role=EventRole.Role.JUDGE)
        role.tracks.set(tracks)
        return user

    def project(self, title, track):
        captain = make_user(f"{title.split()[0].lower()}@teams.example.org")
        team = make_team(self.event, captain, name=f"{title} team")
        return Project.objects.create(event=self.event, team=team, track=track, title=title, tagline="x",
                                      description="y", status=Project.Status.SUBMITTED,
                                      submitted_at=self.event.submissions_close_at - timedelta(hours=1))

    def assign(self, judge, project):
        return Assignment.objects.create(event=self.event, judge=judge, project=project)

    def marks(self, f, q, i):
        keys = {c.key: c.pk for c in self.event.criteria.all()}
        return {keys["functionality"]: f, keys["quality"]: q, keys["innovation"]: i}

    def as_user(self, user):
        client = Client()
        client.force_login(user)
        return client


class IsolationTests(JudgingSetup):
    def setUp(self):
        super().setUp()
        self.assign(self.ada, self.p1)
        self.assign(self.bo, self.p1)
        self.assign(self.cy, self.p3)
        save_score(self.ada, self.event, self.p1.pk, self.marks(5, 4, 3), "Ada's private note", submit=True)
        save_score(self.bo, self.event, self.p1.pk, self.marks(2, 2, 2), "Bo's private note", submit=True)

    def test_a_judge_reads_only_their_own_scores(self):
        data = self.as_user(self.ada).get("/api/judge/scores").json()
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["scores"][0]["comment"], "Ada's private note")
        self.assertNotIn("Bo's private note", json.dumps(data))

    def test_asking_for_a_peer_is_refused_by_the_backend(self):
        client = self.as_user(self.bo)
        for value in (self.ada.external_id, str(self.ada.pk), self.ada.email, "jdg_nobody"):
            response = client.get("/api/judge/scores", {"judge": value})
            self.assertEqual(response.status_code, 403, value)
            self.assertNotIn("Ada's private note", response.content.decode())

    def test_participants_and_strangers_are_refused(self):
        participant = self.p1.team.memberships.get().user
        self.assertEqual(self.as_user(participant).get("/api/judge/scores").status_code, 403)
        self.assertEqual(Client().get("/api/judge/scores").status_code, 401)

    def test_organizers_may_read_any_judge_in_their_event(self):
        response = self.as_user(self.organizer).get("/api/judge/scores", {"judge": self.ada.external_id})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["count"], 1)

    def test_the_scoring_page_never_shows_another_judges_marks(self):
        page = self.as_user(self.bo).get(reverse("judging:score", args=[self.event.slug, self.p1.pk]))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Bo&#x27;s private note")
        self.assertNotContains(page, "Ada")

    def test_another_track_is_invisible_even_by_id(self):
        client = self.as_user(self.ada)
        self.assertEqual(client.get(reverse("judging:score", args=[self.event.slug, self.p3.pk])).status_code, 404)
        response = client.post(f"/api/events/{self.event.slug}/judging/scores",
                               data=json.dumps({"project": self.p3.pk, "criteria": {"functionality": 5}}),
                               content_type="application/json")
        self.assertEqual(response.status_code, 404)
        # Unassigned in their own track is exactly as invisible.
        self.assertEqual(client.get(reverse("judging:score", args=[self.event.slug, self.p2.pk])).status_code, 404)
        self.assertFalse(Score.objects.filter(judge=self.ada, project=self.p3).exists())

    def test_judges_cannot_open_the_organizer_side(self):
        client = self.as_user(self.ada)
        for name in ("progress", "rubric", "judges", "assignments", "results", "exports"):
            self.assertEqual(client.get(reverse(f"judging:{name}", args=[self.event.slug])).status_code, 403, name)
        for stage in ("final-rankings", "raw-scores", "normalized-scores"):
            self.assertEqual(client.get(reverse("judging:export", args=[self.event.slug, stage])).status_code, 403)
        self.assertEqual(client.get(f"/api/events/{self.event.slug}/judging/progress").status_code, 403)

    def test_losing_a_track_takes_unfinished_reviews_away(self):
        self.assign(self.ada, self.p2)
        role = EventRole.objects.get(event=self.event, user=self.ada, role=EventRole.Role.JUDGE)
        removed = staff.update_judge_tracks(self.organizer, role, [self.t2], False)
        self.assertEqual(removed, 1)
        self.assertFalse(Assignment.objects.filter(judge=self.ada, project=self.p2).exists())
        # The submitted score stays in the record.
        self.assertTrue(Score.objects.filter(judge=self.ada, project=self.p1, submitted_at__isnull=False).exists())

    def test_manual_assignment_cannot_cross_tracks_or_conflicts(self):
        with self.assertRaises(JudgingError):
            assign_manually(self.organizer, self.event, self.cy, self.p1)
        staff.add_conflict(self.organizer, self.event, self.ada, self.p2, "Mentored the team")
        with self.assertRaises(JudgingError):
            assign_manually(self.organizer, self.event, self.ada, self.p2)

    def test_people_who_see_every_score_cannot_judge(self):
        with self.assertRaises(JudgingError):
            staff.add_judge(self.organizer, self.event, self.organizer, [self.t1])
        admin = make_user("admin@example.org", role=User.Role.ADMIN)
        with self.assertRaises(JudgingError):
            staff.add_judge(self.organizer, self.event, admin, [self.t1])
        # And a judge can't be made an organizer or an admin while judging.
        client = self.as_user(self.organizer)
        client.post(reverse("events:manage_people", args=[self.event.slug]), {"email": self.ada.email})
        self.assertFalse(EventRole.objects.filter(event=self.event, user=self.ada, role="organizer").exists())
        self.as_user(admin).post(reverse("accounts:admin_user_update", args=[self.ada.pk]),
                                 {"platform_role": "admin", "is_active": "on"})
        self.ada.refresh_from_db()
        self.assertEqual(self.ada.platform_role, User.Role.MEMBER)

    def test_competitors_cannot_judge_their_own_event(self):
        participant = self.p1.team.memberships.get().user
        with self.assertRaises(JudgingError):
            staff.add_judge(self.organizer, self.event, participant, [self.t1])


class ScoringTests(JudgingSetup):
    def setUp(self):
        super().setUp()
        self.assign(self.ada, self.p1)

    def test_drafts_can_be_partial_and_never_count(self):
        keys = {c.key: c.pk for c in self.event.criteria.all()}
        score = save_score(self.ada, self.event, self.p1.pk, {keys["quality"]: 3}, submit=False)
        self.assertIsNone(score.submitted_at)
        self.assertEqual(compute_progress(self.event)["totals"]["submitted"], 0)

    def test_submitting_needs_every_mark_in_range(self):
        keys = {c.key: c.pk for c in self.event.criteria.all()}
        with self.assertRaises(JudgingError):
            save_score(self.ada, self.event, self.p1.pk, {keys["quality"]: 3}, submit=True)
        with self.assertRaises(JudgingError):
            save_score(self.ada, self.event, self.p1.pk, self.marks(6, 3, 3), submit=True)
        save_score(self.ada, self.event, self.p1.pk, self.marks(5, 3, 3), submit=True)

    def test_second_submission_updates_the_same_ballot_and_keeps_history(self):
        save_score(self.ada, self.event, self.p1.pk, self.marks(3, 3, 3), submit=True)
        save_score(self.ada, self.event, self.p1.pk, self.marks(4, 4, 4), submit=False)
        score = Score.objects.get(judge=self.ada, project=self.p1)
        self.assertIsNotNone(score.submitted_at, "a submitted score stays submitted")
        self.assertEqual(Score.objects.filter(judge=self.ada).count(), 1)
        self.assertEqual(ScoreRevision.objects.filter(judge=self.ada).count(), 2)
        self.assertEqual(sorted(score.items.values_list("value", flat=True)), [4, 4, 4])

    def test_judging_waits_for_the_submission_deadline(self):
        Event.objects.filter(pk=self.event.pk).update(submissions_close_at=timezone.now() + timedelta(hours=1))
        response = self.as_user(self.ada).post(
            f"/api/events/{self.event.slug}/judging/scores",
            data=json.dumps({"project": self.p1.pk, "criteria": {"functionality": 3, "quality": 3, "innovation": 3},
                             "submit": True}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"], "judging_not_open")

    def test_judging_end_freezes_scores(self):
        save_score(self.ada, self.event, self.p1.pk, self.marks(3, 3, 3), submit=True)
        Event.objects.filter(pk=self.event.pk).update(judging_ends_at=timezone.now() - timedelta(minutes=1))
        page = self.as_user(self.ada).post(reverse("judging:score", args=[self.event.slug, self.p1.pk]),
                                           {**{f"c_{k}": 5 for k in self.marks(5, 5, 5)}, "action": "submit"})
        self.assertEqual(page.status_code, 403)
        self.assertEqual(sorted(Score.objects.get(judge=self.ada).items.values_list("value", flat=True)), [3, 3, 3])

    def test_the_web_form_submits(self):
        client = self.as_user(self.ada)
        data = {f"c_{pk}": value for pk, value in self.marks(4, 5, 3).items()}
        response = client.post(reverse("judging:score", args=[self.event.slug, self.p1.pk]),
                               {**data, "comment": "Nice", "action": "submit"})
        self.assertEqual(response.status_code, 302)
        self.assertIsNotNone(Score.objects.get(judge=self.ada).submitted_at)

    def test_rate_limit(self):
        from events.models import Activity
        from integrity.limits import LIMITS

        for _ in range(LIMITS["score.save"].limit):
            save_score(self.ada, self.event, self.p1.pk, self.marks(3, 3, 3), submit=False)
        for _ in range(3):
            with self.assertRaises(JudgingError):
                save_score(self.ada, self.event, self.p1.pk, self.marks(3, 3, 3), submit=False)
        # One line in the audit log for the burst, not one per refusal.
        self.assertEqual(Activity.objects.filter(event=self.event, verb="rate.limited", actor=self.ada).count(), 1)

    def test_rubric_locks_once_scoring_starts_but_weights_move(self):
        self.assign(self.bo, self.p1)
        self.assign(self.bo, self.p2)
        save_score(self.ada, self.event, self.p1.pk, self.marks(5, 1, 1), submit=True)
        save_score(self.bo, self.event, self.p1.pk, self.marks(1, 5, 5), submit=True)
        save_score(self.bo, self.event, self.p2.pk, self.marks(3, 3, 3), submit=True)
        client = self.as_user(self.organizer)
        url = reverse("judging:rubric", args=[self.event.slug])
        client.post(url, {"op": "save", "label": "Polish", "weight": "1"})
        self.assertFalse(Criterion.objects.filter(event=self.event, key="polish").exists())
        # One judge's weighted total for one project, so the check doesn't
        # depend on which of two tied projects happens to be listed first.
        def ada_on_p1():
            return compute_results(self.event).raw[str(self.ada.pk)][str(self.p1.pk)]

        before = ada_on_p1()
        functionality = Criterion.objects.get(event=self.event, key="functionality")
        client.post(url, {"op": "save", "id": functionality.pk, "label": "Functionality", "weight": "4",
                          "position": 1})
        functionality.refresh_from_db()
        self.assertEqual(functionality.weight, 4)
        self.assertGreater(ada_on_p1(), before)  # Ada gave functionality a 5, so weighting it up raises her total

    def test_declaring_a_conflict_takes_the_project_away(self):
        response = self.as_user(self.ada).post(reverse("judging:conflict", args=[self.event.slug, self.p1.pk]),
                                               {"reason": "I mentored them"})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Assignment.objects.filter(judge=self.ada, project=self.p1).exists())
        self.assertTrue(Conflict.objects.filter(judge=self.ada, project=self.p1).exists())


class AssignmentBatchTests(JudgingSetup):
    def test_batch_tops_up_within_tracks_and_matches_its_preview(self):
        projects, roles = scoped_inputs(self.event)
        preview = plan_batch(self.event, projects, roles, target=2, seed=42)
        batch, committed = run_batch(self.organizer, self.event, label="Round one", target=2, seed=42, scope="all")
        self.assertEqual(Assignment.objects.count(), preview.total)
        self.assertEqual([(p.pk, [u.pk for u in us]) for p, us in preview.rows],
                         [(p.pk, [u.pk for u in us]) for p, us in committed.rows])
        for a in Assignment.objects.select_related("project"):
            role = EventRole.objects.get(event=self.event, user=a.judge, role="judge")
            self.assertIn(a.project.track, role.tracks.all())
        # Track 2 has only one judge, so its project is one short, and says so.
        self.assertEqual(batch.shortfall_count, 1)

    def test_preview_saves_nothing(self):
        client = self.as_user(self.organizer)
        client.post(reverse("judging:assignments", args=[self.event.slug]),
                    {"op": "preview", "label": "B1", "scope": "gaps", "judges_scope": "all", "target": 2})
        self.assertEqual(Assignment.objects.count(), 0)
        client.post(reverse("judging:assignments", args=[self.event.slug]),
                    {"op": "commit", "label": "B1", "scope": "gaps", "judges_scope": "all", "target": 2, "seed": 7})
        self.assertEqual(AssignmentBatch.objects.get().seed, 7)
        self.assertGreater(Assignment.objects.count(), 0)

    def test_second_batch_never_repeats_a_pair(self):
        run_batch(self.organizer, self.event, label="One", target=1, seed=1, scope="all")
        run_batch(self.organizer, self.event, label="Two", target=2, seed=2, scope="all")
        pairs = list(Assignment.objects.values_list("judge_id", "project_id"))
        self.assertEqual(len(pairs), len(set(pairs)))

    def test_no_assignment_before_submissions_close(self):
        open_event = make_event(slug="still-open")
        with self.assertRaises(Exception):
            run_batch(self.organizer, open_event, label="Too soon", target=1, seed=1, scope="all")

    def test_submitted_reviews_cannot_be_unassigned(self):
        a = self.assign(self.ada, self.p1)
        save_score(self.ada, self.event, self.p1.pk, self.marks(3, 3, 3), submit=True)
        self.as_user(self.organizer).post(reverse("judging:assignments", args=[self.event.slug]),
                                          {"op": "remove", "id": a.pk})
        self.assertTrue(Assignment.objects.filter(pk=a.pk).exists())


class RankingTests(JudgingSetup):
    def test_equal_scores_share_a_rank_whatever_order_they_were_summed_in(self):
        for judge in (self.ada, self.bo):
            for project in (self.p1, self.p2):
                self.assign(judge, project)
        # The same marks in a different order: identical weighted scores, but
        # summed in a different order, which is where float noise creeps in.
        save_score(self.ada, self.event, self.p1.pk, self.marks(4, 3, 4), submit=True)
        save_score(self.ada, self.event, self.p2.pk, self.marks(3, 4, 4), submit=True)
        save_score(self.bo, self.event, self.p1.pk, self.marks(2, 3, 3), submit=True)
        save_score(self.bo, self.event, self.p2.pk, self.marks(3, 3, 2), submit=True)
        rows = {row.project: row for row in compute_results(self.event).rows}
        self.assertEqual(rows[self.p1].raw_rank, rows[self.p2].raw_rank)
        self.assertEqual(rows[self.p1].rank, rows[self.p2].rank)
        self.assertEqual(rows[self.p1].movement, 0)


class ProgressTests(JudgingSetup):
    def test_who_has_not_started(self):
        self.assign(self.ada, self.p1)
        self.assign(self.bo, self.p1)
        save_score(self.bo, self.event, self.p1.pk, self.marks(3, 3, 3), submit=True)
        rows = {row.user: row.status for row in compute_progress(self.event)["judges"]}
        self.assertEqual(rows[self.ada], "not_started")
        self.assertEqual(rows[self.bo], "done")
        self.assertEqual(rows[self.cy], "unassigned")
        page = self.as_user(self.organizer).get(reverse("judging:progress", args=[self.event.slug]) + "?fragment=1")
        self.assertContains(page, "Not started")


class InviteTests(JudgingSetup):
    def test_an_invite_makes_a_judge_once(self):
        invite = staff.create_invite(self.organizer, self.event, "", [self.t2], False)
        newcomer = make_user("new@example.org")
        response = self.as_user(newcomer).post(reverse("judging:invite", args=[invite.token]))
        self.assertRedirects(response, reverse("judging:queue", args=[self.event.slug]))
        role = EventRole.objects.get(event=self.event, user=newcomer, role="judge")
        self.assertEqual(list(role.tracks.all()), [self.t2])
        again = make_user("again@example.org")
        self.as_user(again).post(reverse("judging:invite", args=[invite.token]))
        self.assertFalse(EventRole.objects.filter(user=again).exists())

    def test_email_bound_expired_and_revoked_invites(self):
        bound = staff.create_invite(self.organizer, self.event, "right@example.org", [], True)
        wrong = make_user("wrong@example.org")
        self.as_user(wrong).post(reverse("judging:invite", args=[bound.token]))
        self.assertFalse(EventRole.objects.filter(user=wrong).exists())
        old = staff.create_invite(self.organizer, self.event, "", [], True)
        JudgeInvite.objects.filter(pk=old.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
        self.as_user(wrong).post(reverse("judging:invite", args=[old.token]))
        self.assertFalse(EventRole.objects.filter(user=wrong).exists())
        revoked = staff.create_invite(self.organizer, self.event, "", [], True)
        staff.revoke_invite(self.organizer, revoked)
        self.as_user(wrong).post(reverse("judging:invite", args=[revoked.token]))
        self.assertFalse(EventRole.objects.filter(user=wrong).exists())

    def test_a_competitor_cannot_accept(self):
        participant = self.p1.team.memberships.get().user
        invite = staff.create_invite(self.organizer, self.event, "", [], True)
        self.as_user(participant).post(reverse("judging:invite", args=[invite.token]))
        self.assertFalse(EventRole.objects.filter(user=participant, role="judge").exists())


class ExportTests(JudgingSetup):
    def setUp(self):
        super().setUp()
        self.assign(self.ada, self.p1)
        self.assign(self.bo, self.p1)
        self.assign(self.ada, self.p2)
        save_score(self.ada, self.event, self.p1.pk, self.marks(5, 4, 3), submit=True)
        save_score(self.bo, self.event, self.p1.pk, self.marks(2, 3, 2), submit=True)
        save_score(self.ada, self.event, self.p2.pk, self.marks(3, 3, 3), submit=True)

    def test_every_stage_exports_for_organizers_only(self):
        from judging.exports import STAGE_NAMES

        organizer = self.as_user(self.organizer)
        for stage in STAGE_NAMES:
            response = organizer.get(reverse("judging:export", args=[self.event.slug, stage]))
            self.assertEqual(response.status_code, 200, stage)
            self.assertTrue(response["Content-Type"].startswith("text/csv"))
            self.assertIn(",", response.content.decode().splitlines()[0])
        self.assertEqual(Client().get(reverse("judging:export", args=[self.event.slug, "raw-scores"])).status_code, 401)

    def test_exports_page_is_its_own_console_tab(self):
        # The files cover every stage, so the page sits beside Judging, not inside it.
        url = reverse("judging:exports", args=[self.event.slug])
        self.assertEqual(url, f"/events/{self.event.slug}/manage/exports/")
        page = self.as_user(self.organizer).get(url).content.decode()
        self.assertIn(f'href="{url}" aria-current="page">CSV exports</a>', page)
        self.assertNotIn('aria-label="Judging"', page)
        judging = self.as_user(self.organizer).get(reverse("judging:progress", args=[self.event.slug])).content.decode()
        self.assertNotIn(f'href="{url}" aria-current', judging)

    def test_formula_injection_is_defused_but_numbers_are_not(self):
        Project.objects.filter(pk=self.p1.pk).update(title="=HYPERLINK(\"http://evil\")")
        body = self.as_user(self.organizer).get(
            reverse("judging:export", args=[self.event.slug, "normalized-scores"])).content.decode()
        rows = list(csv.reader(io.StringIO(body)))
        self.assertTrue(any(row[3].startswith("-") for row in rows[1:]), "negative z-scores stay numeric")
        body = self.as_user(self.organizer).get(
            reverse("judging:export", args=[self.event.slug, "raw-scores"])).content.decode()
        self.assertIn("'=HYPERLINK", body)
        self.assertNotIn(',=HYPERLINK', body)


FIXTURE = Path(settings.BASE_DIR) / "fixtures.json"


class FixtureJudgingTests(TestCase):
    """The checker's four T2 probes, against the real seeded data."""

    @classmethod
    def setUpTestData(cls):
        Seeder(demo=True, password="dogfood-demo").run(load_fixture(FIXTURE))

    def probe(self, cookie, path):
        client = Client()
        client.cookies["session"] = cookie
        return client.get(path)

    def test_fixture_scores_are_all_imported(self):
        event = Event.objects.get(external_id="evt_01")
        self.assertEqual(Score.objects.filter(event=event, submitted_at__isnull=False).count(), 126)
        self.assertEqual(list(event.criteria.values_list("key", flat=True)), ["functionality", "quality", "innovation"])
        self.assertEqual(Assignment.objects.filter(event=event, score__isnull=True).count(), 8)

    def test_the_uniform_judge_carries_no_weight(self):
        event = Event.objects.get(external_id="evt_01")
        results = compute_results(event)
        flat = str(User.objects.get(external_id="jdg_07").pk)
        self.assertTrue(all(v == 0 for v in results.z[flat].values()))
        self.assertEqual(results.excluded_duplicates, 4)

    def test_checker_probes(self):
        self.assertEqual(self.probe("jdg_a_8f14c2e6", "/api/judge/scores").status_code, 200)
        self.assertEqual(self.probe("jdg_b_52e0b9d1", "/api/judge/scores?judge=jdg_24").status_code, 403)
        self.assertEqual(self.probe("prt_91d7aa3f", "/api/judge/scores").status_code, 403)
        csv_response = self.probe("org_3c9e51d7a2", "/api/events/sample-hack-2026/export/final-rankings.csv")
        self.assertEqual(csv_response.status_code, 200)
        self.assertIn(",", csv_response.content.decode().splitlines()[0])
