"""Assigning judges to projects.

Batch and algorithmic mode are the same code path with a different scope,
as JUDGING.md describes: an organizer narrows the projects (a track, a hand
picked list) and the judges, or runs it over everything. A run is planned
first (nothing saved, so it can be previewed) and committed with the same
seed, which reproduces the plan exactly unless the data changed in between.
"""

import json
import secrets
from collections import Counter
from dataclasses import dataclass, field

from django.db import transaction

from events.deadline import assert_assignment_allowed, fresh
from events.models import EventRole, log_activity
from projects.models import Project

from . import engine
from .access import can_review_track
from .errors import JudgingError
from .models import Assignment, AssignmentBatch, Conflict, Score


def listed_projects(event):
    return (
        Project.objects.filter(event=event, status=Project.Status.SUBMITTED, duplicate_of__isnull=True)
        .select_related("track", "team")
        .order_by("pk")
    )


def judge_roles(event):
    return (
        EventRole.objects.filter(event=event, role=EventRole.Role.JUDGE)
        .select_related("user")
        .prefetch_related("tracks")
        .order_by("user_id")
    )


@dataclass
class Proposal:
    seed: int
    target: int
    scope: str
    rows: list = field(default_factory=list)          # [(project, [users])] for projects that gain reviewers
    shortfalls: list = field(default_factory=list)    # [(project, missing)]
    load: list = field(default_factory=list)          # [(user, before, after, added)]
    considered: int = 0                               # projects in scope that still needed reviews

    @property
    def total(self):
        return sum(len(users) for _, users in self.rows)


def plan_batch(event, projects, roles, target, seed=None, scope=""):
    """Work out a batch without saving anything."""
    seed = seed if seed is not None else secrets.randbelow(10**9) + 1
    projects = list(projects)
    roles = list(roles)
    existing = list(Assignment.objects.filter(event=event).values_list("judge_id", "project_id"))
    per_project = Counter(project_id for _, project_id in existing)
    per_judge = Counter(judge_id for judge_id, _ in existing)
    blocked = {}
    for judge_id, project_id in existing + list(
        Conflict.objects.filter(event=event).values_list("judge_id", "project_id")
    ):
        blocked.setdefault(judge_id, set()).add(str(project_id))

    judges = [
        engine.Judge(
            id=str(role.user_id),
            tracks={str(t.pk) for t in role.tracks.all()},
            conflicts=blocked.get(role.user_id, set()),
            all_tracks=role.all_tracks,
        )
        for role in roles
    ]
    needed = {str(p.pk): max(target - per_project.get(p.pk, 0), 0) for p in projects}
    plan = engine.assign_judges(
        [engine.Project(id=str(p.pk), track=str(p.track_id)) for p in projects],
        judges,
        reviews_per_project=target,
        seed=seed,
        needed=needed,
        initial_load={str(r.user_id): per_judge.get(r.user_id, 0) for r in roles},
    )

    users = {str(r.user_id): r.user for r in roles}
    by_id = {str(p.pk): p for p in projects}
    proposal = Proposal(seed=seed, target=target, scope=scope,
                        considered=sum(1 for n in needed.values() if n > 0))
    for project in projects:
        chosen = plan.assignment.get(str(project.pk), [])
        if chosen:
            proposal.rows.append((project, [users[j] for j in chosen]))
    proposal.shortfalls = [(by_id[pid], missing) for pid, missing in plan.shortfalls]
    proposal.load = sorted(
        ((users[jid], per_judge.get(int(jid), 0), after, after - per_judge.get(int(jid), 0))
         for jid, after in plan.load.items()),
        key=lambda row: (-row[3], row[0].display_name),
    )
    return proposal


def scoped_inputs(event, *, track=None, project_ids=None, judge_ids=None):
    projects = listed_projects(event)
    if track is not None:
        projects = projects.filter(track=track)
    if project_ids:
        projects = projects.filter(pk__in=project_ids)
    roles = judge_roles(event)
    if judge_ids:
        roles = roles.filter(user_id__in=judge_ids)
    return projects, roles


def run_batch(actor, event, *, label, target, seed, scope, track=None, project_ids=None, judge_ids=None):
    """Plan and save a batch in one transaction, re-reading everything."""
    with transaction.atomic():
        event = fresh(event)
        assert_assignment_allowed(event)
        projects, roles = scoped_inputs(event, track=track, project_ids=project_ids, judge_ids=judge_ids)
        if not roles.exists():
            raise JudgingError("There are no judges to assign. Add or invite judges first.")
        proposal = plan_batch(event, projects, roles, target, seed=seed, scope=scope)
        batch = AssignmentBatch.objects.create(
            event=event, label=label, mode=AssignmentBatch.Mode.ALGORITHMIC, scope=scope, seed=proposal.seed,
            target_reviews=target, shortfall_count=sum(m for _, m in proposal.shortfalls),
            shortfall_detail=json.dumps([[p.pk, m] for p, m in proposal.shortfalls]), created_by=actor,
        )
        Assignment.objects.bulk_create([
            Assignment(event=event, judge=user, project=project, batch=batch, created_by=actor)
            for project, users in proposal.rows for user in users
        ])
        gaps = f"; {batch.shortfall_count} review{'s' if batch.shortfall_count != 1 else ''} couldn't be placed" \
            if batch.shortfall_count else ""
        log_activity(event, actor, "judging.batch",
                     f"ran assignment batch “{label}” ({scope}, seed {proposal.seed}): "
                     f"{proposal.total} new review{'s' if proposal.total != 1 else ''}{gaps}")
    return batch, proposal


def assign_manually(actor, event, judge_user, project):
    with transaction.atomic():
        event = fresh(event)
        assert_assignment_allowed(event)
        role = EventRole.objects.filter(event=event, user=judge_user, role=EventRole.Role.JUDGE) \
            .prefetch_related("tracks").first()
        if role is None:
            raise JudgingError(f"{judge_user.display_name} isn't a judge in this event.")
        if project.event_id != event.pk or not project.is_listed:
            raise JudgingError("Only submitted projects that appear in the gallery can be judged.")
        if not can_review_track(role, project.track_id):
            raise JudgingError(
                f"{judge_user.display_name} doesn't judge {project.track.name if project.track else 'untracked projects'}. "
                "Add the track to their judge settings first."
            )
        if Conflict.objects.filter(judge=judge_user, project=project).exists():
            raise JudgingError(f"{judge_user.display_name} has a recorded conflict with “{project.title}”.")
        if Assignment.objects.filter(judge=judge_user, project=project).exists():
            raise JudgingError(f"{judge_user.display_name} is already reviewing “{project.title}”.")
        assignment = Assignment.objects.create(event=event, judge=judge_user, project=project, created_by=actor)
        log_activity(event, actor, "judging.assigned",
                     f"assigned “{project.title}” to {judge_user.display_name} by hand",
                     team=project.team, project=project)
    return assignment


def remove_assignment(actor, assignment):
    with transaction.atomic():
        if Score.objects.filter(assignment=assignment, submitted_at__isnull=False).exists():
            raise JudgingError("That review already has a submitted score. Scores stay in the record.")
        project, judge = assignment.project, assignment.judge
        assignment.delete()
        log_activity(assignment.event, actor, "judging.unassigned",
                     f"took “{project.title}” off {judge.display_name}'s queue",
                     team=project.team, project=project)
