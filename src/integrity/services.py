"""What an organizer can do about what detect.py finds. Every action needs a
reason and goes in the audit log, and every one can be undone."""

from django.db import transaction
from django.utils import timezone

from events.deadline import fresh
from events.models import log_activity
from projects import comments as comment_rules
from projects.models import Comment, Project
from voting.models import Ballot


class ReviewError(Exception):
    def __init__(self, message):
        super().__init__(message)
        self.message = message


def _reason(reason):
    reason = (reason or "").strip()[:200]
    if not reason:
        raise ReviewError("Say briefly why. It goes in the audit log, so the decision can be checked later.")
    return reason


def exclude_ballots(event, actor, ballot_ids, reason):
    reason = _reason(reason)
    with transaction.atomic():
        event = fresh(event)
        ballots = list(Ballot.objects.select_for_update().filter(event=event, pk__in=ballot_ids,
                                                                  excluded_at__isnull=True))
        if not ballots:
            raise ReviewError("Pick at least one ballot that's still counted.")
        now = timezone.now()
        Ballot.objects.filter(pk__in=[b.pk for b in ballots]).update(
            excluded_at=now, excluded_by=actor, excluded_reason=reason)
    ids = ", ".join(f"#{b.pk}" for b in ballots)
    log_activity(event, actor, "voting.ballot_excluded",
                 f"left {len(ballots)} ballot{'s' if len(ballots) != 1 else ''} out of the count ({ids}): {reason}")
    return len(ballots)


def restore_ballot(event, actor, ballot_id):
    ballot = Ballot.objects.filter(event=event, pk=ballot_id, excluded_at__isnull=False).first()
    if ballot is None:
        raise ReviewError("That ballot is already counted.")
    Ballot.objects.filter(pk=ballot.pk).update(excluded_at=None, excluded_by=None, excluded_reason="")
    log_activity(event, actor, "voting.ballot_restored", f"put ballot #{ballot.pk} back in the count")
    return ballot


def hide_duplicate(event, actor, project_id, original_id, reason):
    """Hide one submission as a duplicate of another, like the fixture's
    prj_41. It leaves the gallery, the vote and the rankings; organizers can
    swap which one is listed from the submissions page."""
    reason = _reason(reason)
    with transaction.atomic():
        project = Project.objects.select_for_update().filter(event=event, pk=project_id,
                                                             duplicate_of__isnull=True).first()
        original = Project.objects.filter(event=event, pk=original_id, duplicate_of__isnull=True).first()
        if project is None or original is None or project.pk == original.pk:
            raise ReviewError("Pick two different submissions that are both still listed.")
        project.duplicate_of = original
        project.save(update_fields=["duplicate_of"])
    log_activity(event, actor, "project.flagged",
                 f"hid “{project.title}” (#{project.pk}) as a duplicate of “{original.title}” (#{original.pk}): {reason}",
                 team=project.team, project=project)
    return project


def remove_repeated(event, actor, comment_ids, reason):
    reason = _reason(reason)
    removed = 0
    for comment in Comment.objects.filter(project__event=event, pk__in=comment_ids, removed_at__isnull=True) \
            .select_related("author", "project__event", "project__team"):
        comment_rules.remove_comment(actor, comment, reason)
        removed += 1
    if not removed:
        raise ReviewError("Those comments are already removed.")
    return removed
