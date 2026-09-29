"""Publishing the judges' results, and what the public and each team see.

Until an organizer publishes, the ranking is theirs alone (JUDGING.md,
section 5). Publishing is a decision, not a date: it needs submissions
closed and a ranking to show, and it ends judging there and then (if it
hadn't ended already), so the published ranking is final and no score can
move under it. Taking the results down hides them again but doesn't reopen
judging; moving the judging end date into the future does both.

Nothing is stored but the decision. The published ranking is computed per
request by results.compute_results, like the organizers' one, from the same
submitted scores.

What a team sees of its own project, when the organizer chose to share
feedback: its average mark per criterion, and the judges' written comments,
in no particular order. Never who wrote them, never another team's.
"""

import random
import statistics

from django.db import transaction
from django.utils import timezone

from events.deadline import fresh, judging_phase
from events.models import log_activity
from events.timeutil import format_dt

from .errors import JudgingError
from .models import JudgingConfig, Score
from .results import compute_results
from .scoring import rubric_for
from .staff import config_for


def state(event, config=None):
    """not_ready (submissions still open, or nothing to rank), unpublished or published."""
    config = config or config_for(event)
    if config.results_published_at and judging_phase(event) == "closed":
        return "published"
    if judging_phase(event) == "not_open":
        return "not_ready"
    return "unpublished"


def is_public(event, config=None):
    return state(event, config) == "published"


def can_see(viewer, event, config=None):
    return viewer.is_organizer or is_public(event, config)


def publish(actor, event, share_feedback=False, announce_winners=False):
    """Publish the ranking. Ends judging now if it's still open, and with
    announce_winners, moves "winners announced" to now if it was later."""
    with transaction.atomic():
        event = fresh(event)
        if judging_phase(event) == "not_open":
            raise JudgingError("Results can be published once submissions have closed and judging has happened.")
        if not compute_results(event).ready:
            raise JudgingError("There's nothing to rank yet: no project has enough submitted scores.")
        now = timezone.now()
        tz = event.tzinfo
        if judging_phase(event, now) == "open":
            old = event.judging_ends_at
            event.judging_ends_at = now
            event.save(update_fields=["judging_ends_at"])
            log_activity(event, actor, "event.dates", f"moved judging ends from {format_dt(old, tz) or 'unset'} to "
                                                       f"{format_dt(now, tz)}, by publishing the results")
        if announce_winners and event.results_at and event.results_at > now:
            old = event.results_at
            event.results_at = now
            event.save(update_fields=["results_at"])
            log_activity(event, actor, "event.dates", f"moved winners announced from {format_dt(old, tz)} to "
                                                       f"{format_dt(now, tz)}, by publishing the results")
        config = JudgingConfig.objects.select_for_update().get(pk=config_for(event).pk)
        config.results_published_at, config.results_published_by = now, actor
        config.share_feedback = bool(share_feedback)
        config.save(update_fields=["results_published_at", "results_published_by", "share_feedback", "updated_at"])
        log_activity(event, actor, "judging.published",
                     "published the judges' results" + (", with feedback for each team" if share_feedback else ""))
    return config


def unpublish(actor, event, why=""):
    config = config_for(event)
    if not config.results_published_at:
        raise JudgingError("The results aren't published.")
    config.results_published_at, config.results_published_by = None, None
    config.save(update_fields=["results_published_at", "results_published_by", "updated_at"])
    log_activity(event, actor, "judging.unpublished", "took the judges' results down" + (f" ({why})" if why else ""))
    return config


def ranking(event, track_id=None):
    """The public ranking: only projects the judges ranked, best first."""
    results = compute_results(event, track_id=track_id)
    if not results.ready:
        return []
    return [row for row in results.rows if row.rank is not None]


def place_of(event, project):
    """(rank, normalized score, how many ranked) for one project, or None."""
    rows = ranking(event)
    for row in rows:
        if row.project.pk == project.pk:
            return {"rank": row.rank, "score": row.display, "of": len(rows), "reviews": row.reviews}
    return None


def page(viewer, event, track_id=None):
    """Everything the results page shows, for this viewer. The API returns
    the same (api/endpoints/judging.py), so the two can't disagree."""
    from events.models import Award
    from projects.services import listed_project
    from records.services import awards_public, public_awards
    from voting import services as vote
    from voting.results import compute_tally

    config = config_for(event)
    ranking_state = state(event, config)
    show_ranking = can_see(viewer, event, config)
    if viewer.is_organizer:
        awards = Award.objects.filter(prize__event=event).select_related("prize__track", "project__team")
    else:
        awards = public_awards(event)
    vote_config = vote.config_for(event)
    vote_state = vote.results_state(event, vote_config) if vote_config and vote_config.is_enabled else None
    show_vote = bool(vote_state) and vote.can_see_results(viewer, event, vote_config)
    yours = None
    if viewer.team is not None:
        project = listed_project(viewer.team)
        if project is not None and project.is_listed:
            yours = {"project": project, "place": place_of(event, project) if is_public(event, config) else None,
                     "feedback": feedback(event, project, config)}
    return {
        "config": config, "state": ranking_state, "show_ranking": show_ranking,
        "preview": viewer.is_organizer and ranking_state != "published",
        "rows": ranking(event, track_id) if show_ranking else [],
        "awards": list(awards.order_by("prize__position", "prize__pk", "pk")),
        "awards_public": awards_public(event), "vote_state": vote_state, "show_vote": show_vote,
        "vote_lines": [line for line in compute_tally(event, vote_config)["lines"] if line["votes"]][:10]
        if show_vote else [],
        "yours": yours,
    }


def feedback(event, project, config=None):
    """What the judges said about one project, without saying who: average
    marks per criterion and the comments, shuffled. None unless shared."""
    config = config or config_for(event)
    if not (is_public(event, config) and config.share_feedback):
        return None
    criteria, _ = rubric_for(event, config)
    scores = list(Score.objects.filter(event=event, project=project, submitted_at__isnull=False)
                  .prefetch_related("items"))
    by_criterion = {c.pk: [] for c in criteria}
    for score in scores:
        for item in score.items.all():
            if item.criterion_id in by_criterion:
                by_criterion[item.criterion_id].append(item.value)
    comments = [s.comment.strip() for s in scores if s.comment.strip()]
    random.Random(project.pk).shuffle(comments)  # the same order every time, but not the judges' order
    return {
        "reviews": len(scores),
        "scale": (config.scale_min, config.scale_max),
        "criteria": [{"label": c.label, "description": c.description,
                      "average": round(statistics.mean(by_criterion[c.pk]), 2) if by_criterion[c.pk] else None}
                     for c in criteria],
        "comments": comments,
    }
