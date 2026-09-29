"""Changes an organizer makes to an event. The console pages and the REST API
both call these, with the same forms, so the two can't disagree about a rule.
Forms validate; these functions write and log."""

from django.db import transaction
from django.utils import timezone
from django.db.models import ProtectedError, RestrictedError

from projects.models import Project

from .models import EventRole, Track, log_activity
from .timeutil import format_dt


class EventError(Exception):
    status = 400
    code = "event_error"

    def __init__(self, message):
        super().__init__(message)
        self.message = message


DATE_LABELS = {
    "starts_at": "kickoff",
    "submissions_close_at": "the submission deadline",
    "judging_ends_at": "the end of judging",
    "results_at": "the results date",
}


def create_event(user, form):
    """form: a valid EventCreateForm."""
    with transaction.atomic():
        event = form.save(commit=False)
        event.created_by = user
        event.save()
        EventRole.objects.create(event=event, user=user, role=EventRole.Role.ORGANIZER, added_by=user)
        for position, name in enumerate(form.cleaned_data.get("tracks_text") or [], start=1):
            Track.objects.create(event=event, name=name, position=position)
        log_activity(event, user, "event.created", f"created {event.name}")
    return event


def assert_deadline_can_move(event, new_deadline, now=None):
    """Once the deadline has passed and judges have scored or people have
    voted, it can't move later: reopening would let projects change under
    scores and ballots already cast, for one team's benefit or everyone's.
    Moving it earlier, or later before anyone has judged, is still allowed
    (an outage, a typo), and every move is logged."""
    from judging.models import Score
    from voting.models import Ballot

    old = event.submissions_close_at
    if new_deadline is None or new_deadline <= old or old > (now or timezone.now()):
        return
    if Score.objects.filter(event=event, submitted_at__isnull=False).exists() or Ballot.objects.filter(event=event).exists():
        raise EventError("Submissions have closed and judging or voting has started, so the deadline can't move "
                         "later: projects would change under scores and votes already given.")


def update_event(user, event, form):
    """form: a valid EventForm bound to event. Every date that moved is logged,
    so "the deadline was changed" can always be traced to someone."""
    # Read the old values from the database: validating a ModelForm has
    # already copied the new ones onto the event object.
    saved = type(event).objects.get(pk=event.pk)
    before = {name: getattr(saved, name) for name in DATE_LABELS}
    was_published, was_early = saved.is_published, saved.gallery_before_deadline
    with transaction.atomic():
        assert_deadline_can_move(saved, form.cleaned_data.get("submissions_close_at"))
        event = form.save()
        tz = event.tzinfo
        for name, label in DATE_LABELS.items():
            old, new = before[name], getattr(event, name)
            if old != new:
                log_activity(event, user, "event.dates",
                             f"moved {label} from {format_dt(old, tz) or 'unset'} to {format_dt(new, tz) or 'unset'}")
        if was_early != event.gallery_before_deadline:
            log_activity(event, user, "event.visibility",
                         "set the gallery to show projects as soon as they're submitted" if event.gallery_before_deadline
                         else "set the gallery to hold projects back until the deadline")
        if was_published != event.is_published:
            log_activity(event, user, "event.visibility",
                         "published the event" if event.is_published else "unpublished the event")
        log_activity(event, user, "event.updated", "saved the event details")
        # Published judges' results are final results. If judging is open
        # again, they aren't, so they come down until someone republishes.
        from judging import publishing
        from judging.models import JudgingConfig
        from events.deadline import judging_phase

        if JudgingConfig.objects.filter(event=event, results_published_at__isnull=False).exists() \
                and judging_phase(event) != "closed":
            publishing.unpublish(user, event, "judging is open again")
    return event


def save_child(user, event, noun, form, editing=None):
    """Save a track, prize or custom question from its valid form."""
    obj = form.save(commit=False)
    obj.event = event
    obj.save()
    form.save_m2m()
    log_activity(event, user, f"{noun}.saved", f"{'updated' if editing else 'added'} the {noun} “{obj}”")
    return obj


def delete_child(user, event, obj, noun, protect_message=""):
    label = str(obj)
    try:
        obj.delete()
    except (ProtectedError, RestrictedError):
        raise EventError(protect_message or f"That {noun} is in use and can't be deleted.")
    log_activity(event, user, f"{noun}.deleted", f"deleted the {noun} “{label}”")
    return label


def add_organizer(user, event, target):
    """target comes from a valid OrganizerForm, which already refused people
    who compete or judge here."""
    role = EventRole.objects.create(event=event, user=target, role=EventRole.Role.ORGANIZER, added_by=user)
    log_activity(event, user, "staff.added", f"added {target.display_name} as an organizer")
    return role


def remove_organizer(user, event, role):
    remaining = event.roles.filter(role=EventRole.Role.ORGANIZER).exclude(pk=role.pk).count()
    if remaining == 0:
        raise EventError("An event needs at least one organizer. Add another before removing this one.")
    name = role.user.display_name
    role.delete()
    log_activity(event, user, "staff.removed", f"removed {name} as {role.role}")
    return name


def promote_duplicate(user, event, pk):
    """Swap which of two submissions from the same team is the listed one."""
    with transaction.atomic():
        duplicate = Project.objects.select_for_update().filter(pk=pk, event=event, duplicate_of__isnull=False).first()
        if duplicate is None:
            raise EventError("That submission isn't flagged as a duplicate.")
        original = Project.objects.select_for_update().get(pk=duplicate.duplicate_of_id)
        # Order matters: the unique constraint allows only one listed project
        # per team, so demote the original before promoting the duplicate.
        Project.objects.filter(pk=original.pk).update(duplicate_of=duplicate)
        Project.objects.filter(duplicate_of=original).exclude(pk=duplicate.pk).update(duplicate_of=duplicate)
        Project.objects.filter(pk=duplicate.pk).update(duplicate_of=None)
        log_activity(event, user, "project.promoted",
                     f"listed “{duplicate.title}” (#{duplicate.pk}) instead of #{original.pk}",
                     team=duplicate.team, project=duplicate)
    return duplicate, original
