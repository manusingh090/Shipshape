"""Saving a judge's marks.

Same shape as saving a submission: one transaction, the event re-read, the
window checked with the server clock, then the write. On top of that, every
save is checked against the assignment gate and the judge's tracks again
(defense in depth), rate limited, and written to the append-only history.
"""

import json
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from events.deadline import assert_judging_open, fresh
from events.models import log_activity
from integrity import limits

from . import engine
from .access import can_review_track, my_assignment
from .errors import JudgingError, NotAssigned, RateLimited
from .models import CriterionScore, Score, ScoreRevision
from .staff import config_for



def rubric_for(event, config=None):
    config = config or config_for(event)
    criteria = list(event.criteria.all())
    rubric = engine.Rubric([
        engine.Criterion(id=str(c.pk), label=c.label, weight=float(c.weight),
                         scale_min=config.scale_min, scale_max=config.scale_max)
        for c in criteria
    ])
    return criteria, rubric


def weighted(score, rubric):
    """A submitted score's weighted total under the current weights, or None
    if it's missing a mark (drafts, or a rubric that grew after scoring)."""
    values = {str(item.criterion_id): item.value for item in score.items.all()}
    try:
        return rubric.weighted_score(values)
    except ValueError:
        return None


def save_score(judge_user, event, project_id, marks, comment="", submit=False):
    """marks: {criterion pk: int}. Returns the Score.

    A draft may be partial and never counts. Submitting needs every mark.
    Once submitted, a score stays submitted: later saves update it (and are
    recorded) but can't turn it back into a draft.
    """
    since = timezone.now() - timedelta(minutes=1)
    limits.check("score.save", judge_user.pk, "That's a lot of saves in one minute. Wait a moment and try again.",
                 count=ScoreRevision.objects.filter(judge=judge_user, created_at__gte=since).count(),
                 exc=RateLimited, event=event, actor=judge_user)
    with transaction.atomic():
        event = fresh(event)
        assert_judging_open(event)
        role, assignment = my_assignment(judge_user, event, project_id)
        if assignment is None or not can_review_track(role, assignment.project.track_id):
            raise NotAssigned()

        config = config_for(event)
        criteria, _ = rubric_for(event, config)
        if not criteria:
            raise JudgingError("The organizers haven't set up the rubric yet.")

        clean = {}
        for criterion in criteria:
            value = marks.get(criterion.pk)
            if value in (None, ""):
                continue
            try:
                value = int(value)
            except (TypeError, ValueError):
                raise JudgingError(f"{criterion.label}: marks are whole numbers.")
            if not config.scale_min <= value <= config.scale_max:
                raise JudgingError(f"{criterion.label}: marks go from {config.scale_min} to {config.scale_max}.")
            clean[criterion.pk] = value

        score = Score.objects.select_for_update().filter(assignment=assignment).first()
        if score is not None and score.submitted_at is not None:
            submit = True
        if submit:
            missing = [c.label for c in criteria if c.pk not in clean]
            if missing:
                raise JudgingError(f"Give a mark for every criterion before submitting. Missing: {', '.join(missing)}.")

        now = timezone.now()
        created = score is None
        was_submitted = score is not None and score.submitted_at is not None
        if created:
            score = Score(assignment=assignment, event=event, judge=judge_user, project=assignment.project,
                          created_at=now)
        score.comment = (comment or "").strip()[:5000]
        score.updated_at = now
        if submit:
            score.submitted_at = now
        score.save()

        for criterion in criteria:
            if criterion.pk in clean:
                CriterionScore.objects.update_or_create(score=score, criterion=criterion,
                                                        defaults={"value": clean[criterion.pk]})
            else:
                CriterionScore.objects.filter(score=score, criterion=criterion).delete()

        ScoreRevision.objects.create(
            event=event, judge=judge_user, project=assignment.project, score=score,
            values_json=json.dumps({c.key: clean[c.pk] for c in criteria if c.pk in clean}),
            comment=score.comment, submitted=submit, actor=judge_user,
        )
        title = assignment.project.title
        if submit and not was_submitted:
            log_activity(event, judge_user, "score.submitted", f"submitted a score for “{title}”",
                         team=assignment.project.team, project=assignment.project)
        elif was_submitted:
            log_activity(event, judge_user, "score.updated", f"changed their submitted score for “{title}”",
                         team=assignment.project.team, project=assignment.project)
    return score


def declare_conflict(judge_user, event, project_id, reason):
    """A judge steps away from an assigned project before scoring it."""
    from .staff import add_conflict

    role, assignment = my_assignment(judge_user, event, project_id)
    if assignment is None:
        raise NotAssigned()
    reason = (reason or "").strip()
    if not reason:
        raise JudgingError("Say briefly why, for example “I mentored this team”.")
    add_conflict(judge_user, event, judge_user, assignment.project, reason)
