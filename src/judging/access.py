"""Role isolation for judging, in one place.

The rules:

* A judge's view of judging data always starts from rows where
  judge = the signed-in user. A judge id or project id sent by the client is
  never trusted for an access decision.
* The assignment table gates existence. A judge who asks for a project they
  weren't assigned gets the same 404 as for a project that doesn't exist, so
  guessing ids reveals nothing, including which tracks other projects are in.
* Track isolation rides on the same gate: assignments are only ever created
  inside a judge's tracks (engine, manual assignment and the service all
  check), pending assignments are removed when a judge loses a track, and the
  track is checked again before any score is written.
* Other judges' scores are never part of a judge's response. Aggregates,
  rankings, progress and exports are organizer-only.

SQLite has no row-level security, so this module is the equivalent layer.
The tests probe it with raw requests (tests/test_judging.py, IsolationTests), and
tests/test_comments.py checks that a team's own history never shows judging.
"""

from events.models import EventRole

from .models import Assignment, Score


def judge_role(user, event):
    if not user.is_authenticated:
        return None
    return (
        EventRole.objects.filter(event=event, user=user, role=EventRole.Role.JUDGE)
        .prefetch_related("tracks")
        .first()
    )


def judged_events(user):
    """Events where this user holds the judge role."""
    from events.models import Event

    if not user.is_authenticated:
        return Event.objects.none()
    return Event.objects.filter(roles__user=user, roles__role=EventRole.Role.JUDGE).distinct()


def track_ids(role):
    return {track.pk for track in role.tracks.all()}


def can_review_track(role, track_id):
    """Floaters may review any track; everyone else only their own."""
    if role is None:
        return False
    return role.all_tracks or (track_id is not None and track_id in track_ids(role))


def my_assignments(user, event):
    """The signed-in judge's queue. Nothing else is ever listed for a judge."""
    return (
        Assignment.objects.filter(event=event, judge=user)
        .select_related("project__team", "project__track", "project__duplicate_of")
        .order_by("project__title")
    )


def my_assignment(user, event, project_id):
    """The gate for opening one project as a judge. Returns None (so the caller
    answers 404) unless this user is a judge here, holds an assignment for the
    project, and may still see its track. A judge who already submitted a
    score keeps read access to it even if their tracks changed since."""
    role = judge_role(user, event)
    if role is None:
        return None, None
    assignment = (
        Assignment.objects.select_related("project__team", "project__track", "project__event")
        .filter(event=event, judge=user, project_id=project_id)
        .first()
    )
    if assignment is None:
        return role, None
    if not can_review_track(role, assignment.project.track_id):
        submitted = Score.objects.filter(assignment=assignment, submitted_at__isnull=False).exists()
        if not submitted:
            return role, None
    return role, assignment


def my_scores(user, event=None):
    scores = Score.objects.filter(judge=user).select_related("project__team", "project__track", "event")
    if event is not None:
        scores = scores.filter(event=event)
    return scores.prefetch_related("items__criterion").order_by("event__name", "project__title")
