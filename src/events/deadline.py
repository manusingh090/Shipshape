"""Deadline enforcement.

Every code path that creates or changes a project, its images, its answers
or a team roster calls one of the assert_* functions inside the same
transaction as the write, after re-reading the event row. So:

* the clock is the server's, never the browser's;
* an organizer moving the deadline takes effect on the very next request;
* there is no second, forgotten door (web form, JSON API and uploads all
  come through here, and the tests poke each one after the deadline).
"""

from django.utils import timezone

from .models import Event
from .timeutil import format_dt


class WindowError(Exception):
    code = "window_closed"

    def __init__(self, event, message):
        super().__init__(message)
        self.event = event
        self.message = message


class SubmissionsClosed(WindowError):
    code = "submissions_closed"


class SubmissionsNotOpen(WindowError):
    code = "submissions_not_open"


class TeamsLocked(WindowError):
    code = "teams_locked"


def fresh(event):
    """Re-read the event inside the current transaction.

    select_for_update is a no-op on SQLite (writes are already serialised by
    BEGIN IMMEDIATE) but it is the right thing if this runs on Postgres.
    """
    return Event.objects.select_for_update().get(pk=event.pk)


def assert_accepting_edits(event, now=None):
    now = now or timezone.now()
    phase = event.phase(now)
    if phase == Event.Phase.CLOSED:
        raise SubmissionsClosed(
            event,
            f"Submissions for {event.name} closed at {format_dt(event.submissions_close_at, event.tzinfo)}. "
            "Projects can't be created or changed any more.",
        )
    if phase == Event.Phase.UPCOMING:
        raise SubmissionsNotOpen(
            event,
            f"Project editing opens at kickoff, {format_dt(event.starts_at, event.tzinfo)}.",
        )


def assert_team_changes_allowed(event, now=None):
    now = now or timezone.now()
    if event.phase(now) == Event.Phase.CLOSED:
        raise TeamsLocked(
            event,
            f"Teams are locked: submissions for {event.name} closed at "
            f"{format_dt(event.submissions_close_at, event.tzinfo)}.",
        )


# --------------------------------------------------------------- judging ----
# Judging runs from the submission deadline (projects are final) until
# judging_ends_at, or indefinitely if the organizer set no end.

class JudgingNotOpen(WindowError):
    code = "judging_not_open"


class JudgingClosed(WindowError):
    code = "judging_closed"


def judging_phase(event, now=None):
    now = now or timezone.now()
    if now < event.submissions_close_at:
        return "not_open"
    if event.judging_ends_at and now >= event.judging_ends_at:
        return "closed"
    return "open"


def assert_judging_open(event, now=None):
    phase = judging_phase(event, now)
    if phase == "not_open":
        raise JudgingNotOpen(
            event,
            f"Judging starts when submissions close, {format_dt(event.submissions_close_at, event.tzinfo)}, "
            "so every judge sees the final version of each project.",
        )
    if phase == "closed":
        raise JudgingClosed(
            event, f"Judging for {event.name} ended at {format_dt(event.judging_ends_at, event.tzinfo)}. Scores are final."
        )


def assert_assignment_allowed(event, now=None):
    now = now or timezone.now()
    if now < event.submissions_close_at:
        raise JudgingNotOpen(
            event,
            "Judges are assigned once submissions close, so the set of projects is final. "
            f"That's {format_dt(event.submissions_close_at, event.tzinfo)}.",
        )


# ---------------------------------------------------------------- voting ----
# The community vote opens when submissions close (or later, if the organizer
# says so), so people vote on final projects, and closes at the organizer's
# closing time. No closing time means it stays open until they set one.

class VotingNotOpen(WindowError):
    code = "voting_not_open"


class VotingClosed(WindowError):
    code = "voting_closed"


class VotingOff(WindowError):
    code = "voting_off"


def voting_opens_at(event, config):
    return config.opens_at or event.submissions_close_at


def voting_phase(event, config, now=None):
    """off, not_open, open or closed. The closing instant counts as closed."""
    now = now or timezone.now()
    if config is None or not config.is_enabled:
        return "off"
    if now < voting_opens_at(event, config):
        return "not_open"
    if config.closes_at and now >= config.closes_at:
        return "closed"
    return "open"


def assert_voting_open(event, config, now=None):
    phase = voting_phase(event, config, now)
    if phase == "off":
        raise VotingOff(event, f"{event.name} isn't running a community vote.")
    if phase == "not_open":
        raise VotingNotOpen(
            event, f"Voting opens {format_dt(voting_opens_at(event, config), event.tzinfo)}, once projects are final."
        )
    if phase == "closed":
        raise VotingClosed(
            event, f"Voting for {event.name} closed at {format_dt(config.closes_at, event.tzinfo)}. Ballots are final."
        )
