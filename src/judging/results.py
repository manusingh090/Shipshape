"""Progress and results, computed from the tables on every request.

Nothing derived is stored, so a weight change or a new score is reflected
immediately and there is no cache to go stale. At hackathon scale (hundreds
of scores) this takes milliseconds.
"""

import statistics
from dataclasses import dataclass, field


from . import engine
from .assigning import judge_roles, listed_projects
from .models import Assignment, AssignmentBatch, Score
from .scoring import rubric_for, weighted
from .staff import config_for


# ------------------------------------------------------------------ progress ----

@dataclass
class JudgeProgress:
    role: object
    assigned: int = 0
    submitted: int = 0
    drafts: int = 0
    last_activity: object = None
    oldest_pending: object = None  # when the oldest review they still owe was assigned

    @property
    def user(self):
        return self.role.user

    @property
    def left(self):
        return self.assigned - self.submitted

    @property
    def status(self):
        """"Not started" is about the work still outstanding: reviews are owed,
        nothing is in draft, and the judge hasn't saved a score since that
        work was handed to them. Earlier finished batches don't count."""
        if self.assigned == 0:
            return "unassigned"
        if self.left == 0:
            return "done"
        touched = self.drafts > 0 or (
            self.last_activity is not None and self.oldest_pending is not None
            and self.last_activity >= self.oldest_pending
        )
        return "in_progress" if touched else "not_started"

    @property
    def status_label(self):
        return {"unassigned": "No assignments", "not_started": "Not started",
                "in_progress": "In progress", "done": "Done"}[self.status]


@dataclass
class ProjectProgress:
    project: object
    target: int
    assigned: int = 0
    submitted: int = 0

    @property
    def status(self):
        if self.submitted >= self.target:
            return "complete"
        if self.assigned < self.target:
            return "short"
        return "waiting"

    @property
    def status_label(self):
        return {"complete": "Fully reviewed", "short": "Needs judges", "waiting": "Waiting on judges"}[self.status]


@dataclass
class BatchProgress:
    batch: object
    total: int = 0
    submitted: int = 0
    not_started: list = field(default_factory=list)


STATUS_ORDER = {"not_started": 0, "in_progress": 1, "unassigned": 2, "done": 3}


def compute_progress(event):
    target = config_for(event).reviews_per_project
    judges = {role.user_id: JudgeProgress(role=role) for role in judge_roles(event)}
    projects = {p.pk: ProjectProgress(project=p, target=target) for p in listed_projects(event)}
    batches = {b.pk: BatchProgress(batch=b) for b in AssignmentBatch.objects.filter(event=event)}
    batch_judges = {}

    scores = {
        row["assignment_id"]: row
        for row in Score.objects.filter(event=event).values("assignment_id", "submitted_at", "updated_at")
    }
    for a in Assignment.objects.filter(event=event).values("id", "judge_id", "project_id", "batch_id", "created_at"):
        score = scores.get(a["id"])
        done = bool(score and score["submitted_at"])
        draft = bool(score and not score["submitted_at"])
        judge = judges.get(a["judge_id"])
        if judge is not None:
            judge.assigned += 1
            judge.submitted += done
            judge.drafts += draft
            if score and (judge.last_activity is None or score["updated_at"] > judge.last_activity):
                judge.last_activity = score["updated_at"]
            if not done and (judge.oldest_pending is None or a["created_at"] < judge.oldest_pending):
                judge.oldest_pending = a["created_at"]
        project = projects.get(a["project_id"])
        if project is not None:
            project.assigned += 1
            project.submitted += done
        if a["batch_id"] in batches:
            batch = batches[a["batch_id"]]
            batch.total += 1
            batch.submitted += done
            touched = batch_judges.setdefault(a["batch_id"], {}).setdefault(a["judge_id"], [0, 0])
            touched[0] += 1
            touched[1] += done or draft

    for batch_id, per_judge in batch_judges.items():
        batches[batch_id].not_started = sorted(
            (judges[jid].user for jid, (count, started) in per_judge.items() if jid in judges and started == 0),
            key=lambda u: u.display_name,
        )

    judge_rows = sorted(judges.values(), key=lambda j: (STATUS_ORDER[j.status], -j.left, j.user.display_name))
    project_rows = sorted(projects.values(), key=lambda p: ({"short": 0, "waiting": 1, "complete": 2}[p.status],
                                                             p.submitted, p.project.title))
    total = sum(j.assigned for j in judges.values())
    done = sum(j.submitted for j in judges.values())
    tracks = {}
    for row in projects.values():
        name = row.project.track.name if row.project.track else "No track"
        entry = tracks.setdefault(name, {"name": name, "projects": 0, "complete": 0, "short": 0})
        entry["projects"] += 1
        entry["complete"] += row.status == "complete"
        entry["short"] += row.status == "short"
    return {
        "target": target,
        "judges": judge_rows,
        "not_started": [j for j in judge_rows if j.status == "not_started"],
        "projects": project_rows,
        "batches": [b for b in batches.values()],
        "tracks": sorted(tracks.values(), key=lambda t: t["name"]),
        "totals": {
            "assignments": total,
            "submitted": done,
            "percent": round(100 * done / total) if total else 0,
            "judges": len(judges),
            "not_started": sum(1 for j in judges.values() if j.status == "not_started"),
            "in_progress": sum(1 for j in judges.values() if j.status == "in_progress"),
            "projects": len(projects),
            "complete": sum(1 for p in projects.values() if p.status == "complete"),
            "short": sum(1 for p in projects.values() if p.status == "short"),
        },
    }


# ------------------------------------------------------------------- results ----

@dataclass
class RankedProject:
    project: object
    reviews: int = 0
    informative: int = 0  # reviews from judges whose marks carry signal (two or more scores that vary)
    raw_mean: float = None
    raw_rank: int = None
    z_mean: float = None
    display: float = None
    rank: int = None

    @property
    def movement(self):
        if self.raw_rank is None or self.rank is None:
            return None
        return self.raw_rank - self.rank

    @property
    def move_size(self):
        return abs(self.movement) if self.movement else 0

    @property
    def thin(self):
        """Ranked on fewer than two informative reviews: treat with care."""
        return self.reviews > 0 and self.informative < 2


@dataclass
class JudgeCalibration:
    user: object
    count: int
    mean: float
    stdev: float
    sigma: float
    bias: float

    @property
    def label(self):
        if self.count == 1:
            return "single score: carries no signal"
        if self.stdev == 0:
            return "same mark every time: carries no signal"
        return ""

    @property
    def lean(self):
        if self.count < 2 or self.stdev == 0:
            return ""
        if self.bias <= -0.5:
            return "harsh"
        if self.bias >= 0.5:
            return "generous"
        return ""


@dataclass
class Results:
    ready: bool
    reason: str = ""
    rows: list = field(default_factory=list)
    calibration: list = field(default_factory=list)
    raw: dict = field(default_factory=dict)
    z: dict = field(default_factory=dict)
    display: dict = field(default_factory=dict)
    pop_mean: float = 0.0
    pop_std: float = 0.0
    kappa: float = 5.0
    score_count: int = 0
    judge_count: int = 0
    incomplete: int = 0
    excluded_duplicates: int = 0
    unreviewed: int = 0


def _competition_ranks(rows, value):
    """1, 2, 2, 4 ranking, higher value first. Equal scores share a rank, and
    values are compared at 9 decimal places so float noise from summing in a
    different order can never turn a tie into a win."""
    ordered = sorted(rows, key=lambda r: -round(value(r), 9))
    ranks, previous, current = {}, None, 0
    for position, row in enumerate(ordered, start=1):
        rounded = round(value(row), 9)
        if rounded != previous:
            current, previous = position, rounded
        ranks[id(row)] = current
    return ranks


def compute_results(event, track_id=None):
    config = config_for(event)
    criteria, rubric = rubric_for(event, config)
    if not criteria:
        return Results(ready=False, reason="There's no rubric yet, so there's nothing to rank.")

    listed = {p.pk: p for p in listed_projects(event)}
    excluded = Score.objects.filter(event=event, submitted_at__isnull=False) \
        .exclude(project_id__in=listed.keys()).count()
    scores = (
        Score.objects.filter(event=event, submitted_at__isnull=False, project_id__in=listed.keys())
        .select_related("judge").prefetch_related("items")
        .order_by("judge_id", "project_id")  # a fixed order, so every database sums the same way
    )
    raw, judges, incomplete = {}, {}, 0
    for score in scores:
        total = weighted(score, rubric)
        if total is None:
            incomplete += 1
            continue
        raw.setdefault(str(score.judge_id), {})[str(score.project_id)] = total
        judges[str(score.judge_id)] = score.judge

    count = sum(len(v) for v in raw.values())
    if count < 2:
        return Results(ready=False, reason="Normalization needs at least two submitted scores.",
                       excluded_duplicates=excluded, incomplete=incomplete)

    z, display = engine.normalize_scores(raw, kappa=config.kappa)
    final = engine.aggregate_project_scores(z)
    everything = [s for per_judge in raw.values() for s in per_judge.values()]
    pop_mean, pop_std = statistics.mean(everything), statistics.pstdev(everything) or 1.0

    per_project = {}
    informative = {}
    for per_judge in raw.values():
        values = list(per_judge.values())
        carries_signal = len(values) >= 2 and statistics.pstdev(values) > 0
        for pid, value in per_judge.items():
            per_project.setdefault(pid, []).append(value)
            informative[pid] = informative.get(pid, 0) + carries_signal

    rows = []
    for pk, project in listed.items():
        if track_id is not None and project.track_id != track_id:
            continue
        values = per_project.get(str(pk), [])
        row = RankedProject(project=project, reviews=len(values), informative=informative.get(str(pk), 0))
        if values:
            row.raw_mean = statistics.mean(values)
            row.z_mean = final[str(pk)]
            row.display = pop_mean + row.z_mean * pop_std
        rows.append(row)

    reviewed = [r for r in rows if r.reviews]
    normalized = _competition_ranks(reviewed, lambda r: r.z_mean)
    by_raw = _competition_ranks(reviewed, lambda r: r.raw_mean)
    for row in reviewed:
        row.rank = normalized[id(row)]
        row.raw_rank = by_raw[id(row)]
    rows.sort(key=lambda r: (r.rank is None, r.rank or 0, -round(r.raw_mean or 0, 9), r.project.title))

    calibration = []
    for jid, per_judge in raw.items():
        values = list(per_judge.values())
        calibration.append(JudgeCalibration(
            user=judges[jid], count=len(values), mean=statistics.mean(values),
            stdev=statistics.stdev(values) if len(values) > 1 else 0.0,
            sigma=engine.judge_spread(values, pop_std, config.kappa),
            bias=statistics.mean(values) - pop_mean,
        ))
    calibration.sort(key=lambda c: (c.bias, c.user.display_name))

    return Results(
        ready=True, rows=rows, calibration=calibration, raw=raw, z=z, display=display,
        pop_mean=pop_mean, pop_std=pop_std, kappa=config.kappa, score_count=count, judge_count=len(raw),
        incomplete=incomplete, excluded_duplicates=excluded, unreviewed=sum(1 for r in rows if not r.reviews),
    )
