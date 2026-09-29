"""The pure engine: rubric, assignment, normalization."""

import contextlib
import io
import statistics
from collections import Counter

from django.test import SimpleTestCase

from judging import engine


class RubricTests(SimpleTestCase):
    def test_weights_need_not_sum_to_one(self):
        rubric = engine.Rubric([engine.Criterion("f", "F", 2), engine.Criterion("q", "Q", 1),
                                engine.Criterion("i", "I", 1)])
        self.assertAlmostEqual(rubric.weighted_score({"f": 5, "q": 3, "i": 1}), (2 * 5 + 3 + 1) / 4)

    def test_missing_mark_is_an_error_not_a_zero(self):
        rubric = engine.Rubric([engine.Criterion("f", "F", 1), engine.Criterion("q", "Q", 1)])
        with self.assertRaises(ValueError):
            rubric.weighted_score({"f": 4})


class AssignmentTests(SimpleTestCase):
    def setUp(self):
        self.projects = [engine.Project(f"p{i}", "a" if i < 12 else "b") for i in range(18)]
        self.judges = [engine.Judge(f"j{i}", {"a"}) for i in range(6)] + \
                      [engine.Judge(f"k{i}", {"b"}) for i in range(3)]

    def test_tracks_are_respected_and_load_is_even(self):
        plan = engine.assign_judges(self.projects, self.judges, reviews_per_project=3, seed=1)
        for project in self.projects:
            reviewers = plan.assignment[project.id]
            self.assertEqual(len(reviewers), 3)
            self.assertEqual(len(set(reviewers)), 3, "nobody reviews a project twice")
            prefix = "j" if project.track == "a" else "k"
            self.assertTrue(all(r.startswith(prefix) for r in reviewers), "never outside the judge's track")
        track_a = [plan.load[j.id] for j in self.judges if "a" in j.tracks]
        self.assertLessEqual(max(track_a) - min(track_a), 1)

    def test_conflicts_are_never_assigned(self):
        self.judges[0].conflicts = {"p0", "p1"}
        plan = engine.assign_judges(self.projects, self.judges, reviews_per_project=5, seed=2)
        self.assertNotIn("j0", plan.assignment["p0"])
        self.assertNotIn("j0", plan.assignment["p1"])

    def test_top_up_counts_existing_load_and_needs(self):
        needed = {p.id: 0 for p in self.projects}
        needed["p0"] = 1
        existing = {j.id: 10 for j in self.judges}
        existing["j5"] = 0  # the least loaded judge should get the extra review
        self.judges[0].conflicts = {"p0"}
        plan = engine.assign_judges(self.projects, self.judges, 3, seed=3, needed=needed, initial_load=existing)
        self.assertEqual(plan.assignment["p0"], ["j5"])
        self.assertEqual(sum(len(v) for v in plan.assignment.values()), 1)

    def test_floaters_can_take_any_track(self):
        judges = [engine.Judge("only-a", {"a"}), engine.Judge("floater", set(), all_tracks=True)]
        plan = engine.assign_judges([engine.Project("x", "b")], judges, reviews_per_project=1, seed=4)
        self.assertEqual(plan.assignment["x"], ["floater"])

    def test_shortfalls_are_reported_not_hidden(self):
        plan = engine.assign_judges([engine.Project("x", "b")], [engine.Judge("k", {"b"})], 3, seed=5)
        self.assertEqual(plan.assignment["x"], ["k"])
        self.assertEqual(plan.shortfalls, [("x", 2)])

    def test_same_seed_same_plan(self):
        a = engine.assign_judges(self.projects, self.judges, 3, seed=99).assignment
        b = engine.assign_judges(self.projects, self.judges, 3, seed=99).assignment
        self.assertEqual(a, b)


class NormalizationTests(SimpleTestCase):
    def test_severity_cancels_out(self):
        # A harsh and a generous judge who agree on the order agree after normalization.
        raw = {"harsh": {"p1": 1.0, "p2": 2.0, "p3": 3.0}, "kind": {"p1": 3.0, "p2": 4.0, "p3": 5.0}}
        z, _ = engine.normalize_scores(raw)
        for project in ("p1", "p2", "p3"):
            self.assertAlmostEqual(z["harsh"][project], z["kind"][project])

    def test_flat_and_single_score_judges_are_neutral(self):
        raw = {"flat": {"p1": 3.0, "p2": 3.0, "p3": 3.0}, "once": {"p4": 1.0},
               "real": {"p1": 2.0, "p2": 4.0, "p3": 5.0, "p4": 3.0}}
        z, _ = engine.normalize_scores(raw)
        self.assertTrue(all(v == 0 for v in z["flat"].values()))
        self.assertEqual(z["once"]["p4"], 0)

    def test_shrinkage_tames_tiny_samples(self):
        # Two scores that barely differ shouldn't become a full standard deviation apart.
        raw = {"small": {"p1": 3.0, "p2": 3.1}, "wide": {"p1": 1.0, "p2": 5.0, "p3": 3.0, "p4": 2.0}}
        plain, _ = engine.normalize_scores(raw, kappa=0)
        shrunk, _ = engine.normalize_scores(raw, kappa=5)
        self.assertAlmostEqual(abs(plain["small"]["p2"]), 0.7071, places=3)  # always 1/sqrt(2) for two scores
        self.assertLess(abs(shrunk["small"]["p2"]), 0.1)

    def test_judges_without_scores_are_skipped(self):
        z, _ = engine.normalize_scores({"idle": {}, "a": {"p1": 2.0, "p2": 4.0}})
        self.assertNotIn("idle", z)

    def test_demo_reproduces_the_reference_run(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            engine._demo()
        text = out.getvalue()
        # These lines are the reference implementation's documented output.
        for line in ("P07          4.42        1     0.97         1         0",
                     "P04          3.14        4    -0.20         7        -3",
                     "P02          1.76       12    -1.30        12         0"):
            self.assertIn(line, text)
        self.assertIn("P04 moved -3: true quality 2.56, judged by J-lenient, J-flat, J-norm-a", text)

    def test_normalization_ranks_closer_to_the_truth(self):
        import random

        rng = random.Random(11)
        better = 0
        for _ in range(60):
            truth, raw, coverage = engine._simulated_event(rng, 40, 6, 3, "reference")
            raw_avg = {p: statistics.mean(raw[j][p] for j in coverage[p]) for p in truth}
            z, _ = engine.normalize_scores(raw)
            better += engine.spearman(truth, engine.aggregate_project_scores(z)) > engine.spearman(truth, raw_avg)
        self.assertGreater(better, 50)


class LoadBalanceOnRealisticInput(SimpleTestCase):
    def test_multi_track_judges_still_spread_evenly(self):
        tracks = ["t1", "t2", "t3"]
        projects = [engine.Project(f"p{i}", tracks[i % 3]) for i in range(30)]
        judges = [engine.Judge(f"j{i}", {tracks[i % 3], tracks[(i + 1) % 3]}) for i in range(9)]
        plan = engine.assign_judges(projects, judges, 3, seed=8)
        load = Counter(j for reviewers in plan.assignment.values() for j in reviewers)
        self.assertLessEqual(max(load.values()) - min(load.values()), 2)
        self.assertFalse(plan.shortfalls)
