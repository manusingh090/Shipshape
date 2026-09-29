"""
Judging engine: weighted rubric scoring, load-balanced judge assignment and
cross-judge normalization.

Pure standard library, no Django, so it can be read, tested and run on its
own. Ported from the reference `scoring_engine.py` supplied with the T2 brief;
JUDGING.md explains the reasoning behind each piece.

    python judging/engine.py                 the worked example (reference demo)
    python judging/engine.py --trials 1000   the Monte Carlo proof in JUDGING.md

Changes from the reference, and why:

* assign_judges returns an AssignmentPlan (assignments, shortfalls, final load)
  instead of printing a warning. The portal stores shortfalls on the batch and
  in the activity log.
* assign_judges takes `needed` (reviews still missing per project) and
  `initial_load` (reviews each judge already carries), so a second batch tops
  coverage up instead of starting from zero, and it supports floater judges who
  may review any track. With neither argument it behaves exactly like the
  reference, down to the random numbers it draws.
* normalize_scores skips judges who have no scores instead of crashing.
* The demo no longer claims per-judge z spread is "~1.0 by construction". With
  shrunk variances it is below 1 for any judge whose own spread is smaller than
  the population's, and exactly 0 for a flat grader.
"""

from __future__ import annotations

import argparse
import random
import statistics
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple


# ---------------------------------------------------------------------------
# 1. Rubric and weighted scoring
# ---------------------------------------------------------------------------

@dataclass
class Criterion:
    id: str
    label: str
    weight: float  # organizer-set; need not sum to 1, normalized below
    scale_min: int = 1
    scale_max: int = 5


@dataclass
class Rubric:
    criteria: List[Criterion]

    def normalized_weights(self) -> Dict[str, float]:
        total = sum(c.weight for c in self.criteria)
        if total <= 0:
            raise ValueError("rubric weights must sum to > 0")
        return {c.id: c.weight / total for c in self.criteria}

    def weighted_score(self, criterion_scores: Dict[str, float]) -> float:
        """criterion_scores: {criterion_id: raw score on the rubric's scale}.
        Every criterion shares one scale, so the weighted sum stays on it."""
        w = self.normalized_weights()
        missing = set(w) - set(criterion_scores)
        if missing:
            raise ValueError(f"missing scores for criteria: {missing}")
        return sum(w[cid] * criterion_scores[cid] for cid in w)


# ---------------------------------------------------------------------------
# 2. Judge assignment: load-balanced, track-isolated, conflict-aware
# ---------------------------------------------------------------------------

@dataclass
class Judge:
    id: str
    tracks: Set[str]  # tracks this judge is eligible to review
    conflicts: Set[str] = field(default_factory=set)  # project ids to never assign
    all_tracks: bool = False  # a floater, made so explicitly by an organizer


@dataclass
class Project:
    id: str
    track: str


@dataclass
class AssignmentPlan:
    assignment: Dict[str, List[str]]  # project id -> judge ids, in pick order
    shortfalls: List[Tuple[str, int]]  # (project id, reviews still missing)
    load: Dict[str, int]  # judge id -> reviews carried after this plan


def assign_judges(
    projects: List[Project],
    judges: List[Judge],
    reviews_per_project: int = 3,
    seed: Optional[int] = None,
    needed: Optional[Dict[str, int]] = None,
    initial_load: Optional[Dict[str, int]] = None,
) -> AssignmentPlan:
    """
    Give each project `needed[project]` new reviewers (default
    reviews_per_project) unless the eligible pool for its track is genuinely
    smaller, in which case the gap is reported, never silently dropped.

    Guarantees:
      - a judge never reviews a project outside judge.tracks (unless a floater)
      - a judge never reviews a project in judge.conflicts; callers put every
        project a judge already reviews in there, so nobody reviews one twice
      - every pick goes to whoever, among eligible non-conflicted judges,
        currently carries the fewest reviews (initial_load counts)
      - no bias from submission order or judge order (both shuffled)
    """
    rng = random.Random(seed)
    load: Dict[str, int] = {j.id: 0 for j in judges}
    for judge_id, count in (initial_load or {}).items():
        if judge_id in load:
            load[judge_id] = count

    by_track: Dict[str, List[Judge]] = {}
    floaters: List[Judge] = []
    for j in judges:
        if j.all_tracks:
            floaters.append(j)
            continue
        for t in sorted(j.tracks):
            by_track.setdefault(t, []).append(j)

    order = projects[:]
    rng.shuffle(order)

    assignment: Dict[str, List[str]] = {p.id: [] for p in projects}
    shortfalls: List[Tuple[str, int]] = []

    for p in order:
        want = reviews_per_project if needed is None else needed.get(p.id, reviews_per_project)
        if want <= 0:
            continue
        pool = [j for j in by_track.get(p.track, []) + floaters if p.id not in j.conflicts]
        rng.shuffle(pool)
        pool.sort(key=lambda j: load[j.id])
        chosen = pool[:want]
        for j in chosen:
            load[j.id] += 1
        assignment[p.id] = [j.id for j in chosen]
        if len(chosen) < want:
            shortfalls.append((p.id, want - len(chosen)))

    return AssignmentPlan(assignment=assignment, shortfalls=shortfalls, load=load)


# ---------------------------------------------------------------------------
# 3. Cross-judge normalization: shrinkage z-score
# ---------------------------------------------------------------------------

def normalize_scores(
    raw: Dict[str, Dict[str, float]],  # {judge_id: {project_id: raw_score}}
    kappa: float = 5.0,  # prior strength, in "pseudo-projects" of population spread
) -> Tuple[Dict[str, Dict[str, float]], Dict[str, Dict[str, float]]]:
    """
    Returns (z_scores, display_scores).

    z_scores[judge][project]        standardized against that judge's own mean
                                    and (shrunk) spread
    display_scores[judge][project]  z mapped back onto the population's raw
                                    scale, so organizers see a 1 to 5 number

    Each judge's variance is shrunk toward the population's, weighted by how
    much data the judge has:

        sigma_j^2 = ((n_j - 1) * sigma_j_raw^2 + kappa * sigma_pop^2)
                    / (n_j - 1 + kappa)

    That avoids the two failure modes of plain z-scores: a wild spread
    estimate from a judge with very few scores, and a divide-by-zero from a
    judge whose scores never vary. That flat judge still gets a finite sigma,
    and because every one of their scores equals their mean, every z they
    produce is exactly 0: a neutral vote, not a crash and not a pull toward
    the middle. A judge with a single score is neutral for the same reason.
    """
    raw = {judge: scores for judge, scores in raw.items() if scores}
    all_scores = [s for proj_scores in raw.values() for s in proj_scores.values()]
    if len(all_scores) < 2:
        raise ValueError("need at least 2 scores total to normalize")
    pop_mean = statistics.mean(all_scores)
    pop_std = statistics.pstdev(all_scores) or 1.0  # guard an all-identical field

    z: Dict[str, Dict[str, float]] = {}
    display: Dict[str, Dict[str, float]] = {}

    for judge_id, scores in raw.items():
        values = list(scores.values())
        n = len(values)
        mu_j = statistics.mean(values)
        sigma_j_raw = statistics.stdev(values) if n >= 2 else 0.0

        if n >= 2:
            sigma_j2 = ((n - 1) * sigma_j_raw**2 + kappa * pop_std**2) / (n - 1 + kappa)
        else:
            sigma_j2 = pop_std**2  # one data point: borrow the population's spread wholesale
        sigma_j = sigma_j2**0.5 or pop_std

        z[judge_id] = {}
        display[judge_id] = {}
        for project_id, raw_score in scores.items():
            zscore = (raw_score - mu_j) / sigma_j
            z[judge_id][project_id] = zscore
            display[judge_id][project_id] = pop_mean + zscore * pop_std

    return z, display


def judge_spread(values: List[float], pop_std: float, kappa: float) -> float:
    """The shrunk sigma normalize_scores uses for one judge, for reporting."""
    n = len(values)
    if n >= 2:
        raw_sd = statistics.stdev(values)
        return (((n - 1) * raw_sd**2 + kappa * pop_std**2) / (n - 1 + kappa)) ** 0.5
    return pop_std


def aggregate_project_scores(z_scores: Dict[str, Dict[str, float]]) -> Dict[str, float]:
    """Final ranking score per project: mean z across judges who scored it.
    Equal weight by design: JUDGING.md explains why reliability weighting was
    considered and rejected as the default."""
    per_project: Dict[str, List[float]] = {}
    for judge_scores in z_scores.values():
        for project_id, zscore in judge_scores.items():
            per_project.setdefault(project_id, []).append(zscore)
    return {p: statistics.mean(zs) for p, zs in per_project.items()}


# ---------------------------------------------------------------------------
# 4. CSV export: one shape, reused for every stage
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 5. Proof: the worked example, and a Monte Carlo check against known truth
# ---------------------------------------------------------------------------

DISAGREEMENT = 1.5  # how far (in z) one judge's view of a project sits from the rest of its panel


def panel_gaps(z_scores: Dict[str, Dict[str, float]]) -> Dict[str, Dict[str, float]]:
    """For every project with three or more judges: each judge's normalized
    score minus the mean of the other judges' on that project. A large gap
    is what a judge boosting a friend, or sinking a rival, looks like; it's
    also what honest disagreement looks like, which is why it's only shown."""
    panels: Dict[str, Dict[str, float]] = {}
    for judge, per_project in z_scores.items():
        for project, z in per_project.items():
            panels.setdefault(project, {})[judge] = z
    gaps: Dict[str, Dict[str, float]] = {}
    for project, panel in panels.items():
        if len(panel) < 3:
            continue
        total = sum(panel.values())
        gaps[project] = {j: z - (total - z) / (len(panel) - 1) for j, z in panel.items()}
    return gaps


def spearman(a: Dict[str, float], b: Dict[str, float]) -> float:
    """Rank correlation between two scorings of the same keys (no tie handling;
    the simulations use continuous true qualities)."""
    keys = list(a)
    rank_a = {k: r for r, k in enumerate(sorted(keys, key=lambda k: a[k]))}
    rank_b = {k: r for r, k in enumerate(sorted(keys, key=lambda k: b[k]))}
    n = len(keys)
    d2 = sum((rank_a[k] - rank_b[k]) ** 2 for k in keys)
    return 1 - 6 * d2 / (n * (n * n - 1))


def _demo() -> None:
    """The reference demo: harsh, lenient, flat and three ordinary judges,
    twelve projects, three reviews each. Output is identical to the reference."""
    rng = random.Random(42)
    project_ids = [f"P{i:02d}" for i in range(1, 13)]
    true_quality = {p: rng.uniform(2.0, 4.5) for p in project_ids}

    judges = {
        "J-harsh": lambda q: max(1, min(5, q - 1.3 + rng.gauss(0, 0.4))),
        "J-lenient": lambda q: max(1, min(5, q + 1.0 + rng.gauss(0, 0.3))),
        "J-flat": lambda q: 3.0,
        "J-norm-a": lambda q: max(1, min(5, q + rng.gauss(0, 0.6))),
        "J-norm-b": lambda q: max(1, min(5, q + rng.gauss(0, 0.6))),
        "J-norm-c": lambda q: max(1, min(5, q + rng.gauss(0, 0.6))),
    }

    projects = [Project(id=p, track="general") for p in project_ids]
    demo_judges = [Judge(id=jid, tracks={"general"}) for jid in judges]
    coverage = assign_judges(projects, demo_judges, reviews_per_project=3, seed=7).assignment

    raw: Dict[str, Dict[str, float]] = {jid: {} for jid in judges}
    for p in project_ids:
        q = true_quality[p]
        for jid in coverage[p]:
            raw[jid][p] = round(judges[jid](q), 2)

    z, display = normalize_scores(raw)
    raw_final = {p: statistics.mean(raw[j][p] for j in coverage[p]) for p in project_ids}
    norm_final = aggregate_project_scores(z)

    raw_rank = {p: r for r, p in enumerate(sorted(project_ids, key=lambda p: -raw_final[p]), 1)}
    norm_rank = {p: r for r, p in enumerate(sorted(project_ids, key=lambda p: -norm_final[p]), 1)}

    print(f"{'Project':8} {'RawAvg':>8} {'RawRank':>8} {'NormZ':>8} {'NormRank':>9} {'Movement':>9}")
    for p in sorted(project_ids, key=lambda p: norm_rank[p]):
        move = raw_rank[p] - norm_rank[p]
        arrow = f"+{move}" if move > 0 else str(move)
        print(f"{p:8} {raw_final[p]:8.2f} {raw_rank[p]:8} {norm_final[p]:8.2f} {norm_rank[p]:9} {arrow:>9}")

    print("\nWho each big mover drew (and its hidden true quality):")
    for p in sorted(project_ids, key=lambda p: norm_rank[p]):
        move = raw_rank[p] - norm_rank[p]
        if abs(move) >= 2:
            print(f"  {p} moved {move:+d}: true quality {true_quality[p]:.2f}, judged by {', '.join(coverage[p])}")

    print("\nPer-judge severity and spread:")
    raw_all = [s for d in raw.values() for s in d.values()]
    pop_std = statistics.pstdev(raw_all)
    for jid in judges:
        vals = list(raw[jid].values())
        zs = list(z[jid].values())
        print(f"  {jid:10} mean={statistics.mean(vals):.2f}  raw stdev={statistics.stdev(vals):.2f}  "
              f"shrunk sigma={judge_spread(vals, pop_std, 5.0):.2f}  z stdev={statistics.pstdev(zs):.2f}")
    print(f"\nraw pooled stdev: {pop_std:.2f}. Per-judge z spread is below 1 wherever shrinkage pulled a")
    print("judge's sigma up toward the population's, and exactly 0 for the flat judge.")
    print(f"\nrank correlation with true quality: raw {spearman(true_quality, raw_final):.3f}, "
          f"normalized {spearman(true_quality, norm_final):.3f}")

    load = {jid: 0 for jid in judges}
    for reviewers in coverage.values():
        for jid in reviewers:
            load[jid] += 1
    print("\nreviews per judge (load balance from assign_judges):", load)


def _simulated_event(rng: random.Random, n_projects: int, n_judges: int, k: int, kind: str):
    """One synthetic event with a hidden true quality per project."""
    project_ids = [f"P{i:02d}" for i in range(n_projects)]
    truth = {p: rng.uniform(2.0, 4.5) for p in project_ids}
    if kind == "reference":
        behaviour = {
            "harsh": (-1.3, 0.4, False), "lenient": (1.0, 0.3, False), "flat": (0.0, 0.0, True),
            "a": (0.0, 0.6, False), "b": (0.0, 0.6, False), "c": (0.0, 0.6, False),
        }
    else:  # "fixture-like": many judges, few scores each, whole-number marks
        behaviour = {f"J{i:02d}": (rng.gauss(0, 0.6), 0.5, rng.random() < 0.07) for i in range(n_judges)}
    plan = assign_judges([Project(p, "g") for p in project_ids], [Judge(j, {"g"}) for j in behaviour],
                         reviews_per_project=k, seed=rng.randrange(1 << 30))
    raw: Dict[str, Dict[str, float]] = {j: {} for j in behaviour}
    for p in project_ids:
        for j in plan.assignment[p]:
            bias, noise, flat = behaviour[j]
            if flat:
                score = 3.0 if kind == "reference" else 4.0
            else:
                score = max(1.0, min(5.0, truth[p] + bias + rng.gauss(0, noise)))
                if kind != "reference":
                    score = float(round(score))
            raw[j][p] = score
    return truth, raw, plan.assignment


def proof(trials: int = 1000, seed: int = 2026) -> None:
    """Does normalization rank projects closer to their true quality than a
    raw average? Measured, not asserted, over many simulated events."""
    scenarios = [
        ("reference judges, 12 projects", "reference", 12, 6),
        ("reference judges, 40 projects", "reference", 40, 6),
        ("fixture-like: 30 judges, 40 projects", "fixture", 40, 30),
    ]
    methods = [("raw average", None), ("plain z-score", 0.01), ("shrinkage, kappa=5", 5.0), ("shrinkage, kappa=25", 25.0)]
    rng = random.Random(seed)
    print(f"Rank correlation with the hidden true quality, mean over {trials} simulated events")
    print("(1.0 would be a perfect ranking; every event gets 3 reviews per project)\n")
    header = "".join(f"{name:>22}" for name, _ in methods)
    print(f"{'scenario':38}{header}   kappa=5 beats raw")
    for label, kind, n_projects, n_judges in scenarios:
        totals = {name: 0.0 for name, _ in methods}
        wins = 0
        for _ in range(trials):
            truth, raw, coverage = _simulated_event(rng, n_projects, n_judges, 3, kind)
            raw_avg = {p: statistics.mean(raw[j][p] for j in coverage[p]) for p in truth}
            results = {}
            for name, kappa in methods:
                if kappa is None:
                    results[name] = spearman(truth, raw_avg)
                else:
                    z, _ = normalize_scores(raw, kappa=kappa)
                    results[name] = spearman(truth, aggregate_project_scores(z))
                totals[name] += results[name]
            wins += results["shrinkage, kappa=5"] > results["raw average"]
        cells = "".join(f"{totals[name] / trials:>22.3f}" for name, _ in methods)
        print(f"{label:38}{cells}   {wins / trials:>6.0%}")


def _rank_of(project: str, scores: Dict[str, float]) -> int:
    ordered = sorted(scores, key=lambda p: -scores[p])
    return ordered.index(project) + 1


def collusion(trials: int = 1000, seed: int = 2026) -> None:
    """What judges on a friend's panel can do to the ranking, and how often
    panel_gaps shows it. Fixture-like events (30 judges, 40 projects, 3
    reviews, whole marks); the friend is a random bottom-half project."""
    rng = random.Random(seed)
    thresholds = (1.0, 1.25, 1.5, 1.75, 2.0)
    caught = {}   # colluders -> threshold -> events where the friend's panel is flagged
    honest = {t: 0 for t in thresholds}   # honest projects flagged, over the no-collusion events
    honest_projects = 0
    print(f"Judge collusion, {trials} simulated fixture-like events per row")
    print(f"(flagged: some judge on the friend's panel is {DISAGREEMENT} or more from the rest of it)\n")
    print(f"{'scenario':44}{'median rank':>12}{'top 3':>8}{'top 10':>8}{'flagged':>9}")
    for label, colluders in [("honest panel (the friend's true place)", 0),
                             ("1 of 3 judges gives the friend a 5", 1),
                             ("2 of 3 judges give the friend a 5", 2)]:
        ranks, top3, top10 = [], 0, 0
        caught[colluders] = {t: 0 for t in thresholds}
        for _ in range(trials):
            truth, raw, coverage = _simulated_event(rng, 40, 30, 3, "fixture")
            friend = rng.choice(sorted(truth, key=lambda p: truth[p])[:20])
            for j in coverage[friend][:colluders]:
                raw[j][friend] = 5.0
            z, _ = normalize_scores(raw, kappa=5.0)
            gaps = panel_gaps(z)
            rank = _rank_of(friend, aggregate_project_scores(z))
            ranks.append(rank)
            top3 += rank <= 3
            top10 += rank <= 10
            worst = max((abs(g) for g in gaps.get(friend, {}).values()), default=0.0)
            for t in thresholds:
                caught[colluders][t] += worst >= t
            if colluders == 0:
                honest_projects += len(gaps)
                for per in gaps.values():
                    extreme = max(abs(g) for g in per.values())
                    for t in thresholds:
                        honest[t] += extreme >= t
        print(f"{label:44}{statistics.median(ranks):>12.0f}{top3 / trials:>8.0%}{top10 / trials:>8.0%}"
              f"{caught[colluders][DISAGREEMENT] / trials:>9.0%}")
    print(f"\nWith nobody colluding, {honest[DISAGREEMENT] / honest_projects:.1%} of projects are flagged anyway "
          f"({honest[DISAGREEMENT] / trials:.1f} of 40 per event): honest disagreement, for an organizer to read.")
    print(f"\n{'threshold':>10}{'1 colluder caught':>19}{'2 colluders caught':>20}{'honest projects flagged':>25}")
    for t in thresholds:
        print(f"{t:>10}{caught[1][t] / trials:>19.0%}{caught[2][t] / trials:>20.0%}"
              f"{honest[t] / honest_projects:>25.1%}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Judging engine worked example and proof")
    parser.add_argument("--trials", type=int, default=0, help="run the Monte Carlo proof with this many events")
    parser.add_argument("--seed", type=int, default=2026)
    parser.add_argument("--collusion", action="store_true", help="with --trials: simulate colluding judges instead")
    args = parser.parse_args()
    if args.trials and args.collusion:
        collusion(args.trials, args.seed)
    elif args.trials:
        proof(args.trials, args.seed)
    else:
        _demo()


if __name__ == "__main__":
    main()
