from datetime import timezone as dt_timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.db import models
from django.urls import reverse
from django.utils import timezone
from django.utils.timezone import now as utc_now

TRACK_COLOURS = 8


class Event(models.Model):
    """A hackathon.

    The dates are the rules. `phase()` is the single place that decides
    whether projects may still change, and every write path asks it (see
    events/deadline.py).
    """

    class Phase(models.TextChoices):
        UPCOMING = "upcoming", "Not started"
        OPEN = "open", "Open"
        CLOSED = "closed", "Closed"

    slug = models.SlugField(max_length=80, unique=True)
    name = models.CharField(max_length=140)
    tagline = models.CharField(max_length=200, blank=True)
    description = models.TextField(blank=True, help_text="Markdown: **bold**, _italic_, lists, links.")
    location = models.CharField(max_length=140, blank=True)
    timezone = models.CharField(
        max_length=64, default="UTC", help_text="Dates on the event page are shown in this time zone."
    )
    starts_at = models.DateTimeField("kickoff", help_text="Project editing opens at kickoff.")
    submissions_close_at = models.DateTimeField(
        "submission deadline", help_text="After this moment nobody can create or change a project."
    )
    judging_ends_at = models.DateTimeField(null=True, blank=True)
    results_at = models.DateTimeField("winners announced", null=True, blank=True)
    max_team_size = models.PositiveSmallIntegerField(default=4)
    is_published = models.BooleanField(
        "published", default=False, help_text="Unpublished events are only visible to organizers."
    )
    comments_enabled = models.BooleanField(
        "comments on projects", default=True,
        help_text="Signed-in people can comment on projects in the gallery. Turn off to close every thread.",
    )
    gallery_before_deadline = models.BooleanField(
        "show projects in the gallery before the deadline", default=False,
        help_text="Off: submitted projects stay private to their team and the organizers until submissions "
                  "close, so nobody can copy an early team's idea. On: they appear as soon as they're submitted.",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    # (The field above is called timezone, so the module can't be named here.)
    created_at = models.DateTimeField(default=utc_now)
    updated_at = models.DateTimeField(auto_now=True)
    external_id = models.CharField(max_length=40, blank=True, default="", db_index=True)

    class Meta:
        ordering = ["-submissions_close_at"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(submissions_close_at__gt=models.F("starts_at")),
                name="event_deadline_after_kickoff",
            ),
        ]

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse("events:detail", args=[self.slug])

    @property
    def tzinfo(self):
        try:
            return ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            return dt_timezone.utc

    def gallery_open(self, now=None):
        """Whether submitted projects are public yet (see gallery_before_deadline)."""
        return self.gallery_before_deadline or (now or utc_now()) >= self.submissions_close_at

    def phase(self, now=None):
        now = now or timezone.now()
        if now < self.starts_at:
            return self.Phase.UPCOMING
        if now < self.submissions_close_at:
            return self.Phase.OPEN
        return self.Phase.CLOSED

    @property
    def is_open(self):
        return self.phase() == self.Phase.OPEN

    @property
    def is_closed(self):
        return self.phase() == self.Phase.CLOSED

    @property
    def is_upcoming(self):
        return self.phase() == self.Phase.UPCOMING

    def milestones(self, now=None):
        """Key dates in order, marking where "now" falls.

        The milestone whose segment contains now gets current=True and a
        step from 1 to 9 (tenths of the way to the next date; 10 after the
        last date). Steps become CSS classes, because the content security
        policy rules out inline styles.
        """
        now = now or timezone.now()
        items = [
            {"label": "Kickoff", "at": self.starts_at, "key": "kickoff"},
            {"label": "Submissions close", "at": self.submissions_close_at, "key": "deadline"},
        ]
        if self.judging_ends_at:
            items.append({"label": "Judging ends", "at": self.judging_ends_at, "key": "judging"})
        if self.results_at:
            items.append({"label": "Winners announced", "at": self.results_at, "key": "results"})
        items.sort(key=lambda m: m["at"])
        for item in items:
            item["past"] = item["at"] <= now
        passed = [i for i, item in enumerate(items) if item["past"]]
        if not passed:
            items[0]["before"] = True
            return items
        index = passed[-1]
        current = items[index]
        current["current"] = True
        if index + 1 < len(items):
            span = (items[index + 1]["at"] - current["at"]).total_seconds()
            fraction = (now - current["at"]).total_seconds() / span if span > 0 else 1
            current["step"] = max(1, min(9, round(fraction * 10)))
        else:
            current["step"] = 10
        return items


class EventRole(models.Model):
    class Role(models.TextChoices):
        ORGANIZER = "organizer", "Organizer"
        JUDGE = "judge", "Judge"

    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="roles")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="event_roles")
    role = models.CharField(max_length=16, choices=Role.choices)
    # A judge only ever reviews projects in these tracks...
    tracks = models.ManyToManyField("Track", blank=True, related_name="judges")
    # ...unless an organizer explicitly made them a floater.
    all_tracks = models.BooleanField(
        "can judge every track", default=False,
        help_text="Floaters can be assigned projects from any track.",
    )
    added_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["role", "user__name"]
        constraints = [
            models.UniqueConstraint(fields=["event", "user", "role"], name="one_role_row_per_person"),
        ]

    def __str__(self):
        return f"{self.user} ({self.role}) at {self.event}"


class Track(models.Model):
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="tracks")
    name = models.CharField(max_length=80)
    description = models.CharField(max_length=300, blank=True)
    position = models.PositiveSmallIntegerField(default=0)
    external_id = models.CharField(max_length=40, blank=True, default="", db_index=True)

    class Meta:
        ordering = ["position", "id"]
        constraints = [
            models.UniqueConstraint(fields=["event", "name"], name="track_name_unique_per_event"),
        ]

    def __str__(self):
        return self.name

    @property
    def colour_index(self):
        return self.position % TRACK_COLOURS


class Prize(models.Model):
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="prizes")
    track = models.ForeignKey(Track, null=True, blank=True, on_delete=models.SET_NULL, related_name="prizes")
    name = models.CharField(max_length=120)
    value = models.CharField(max_length=80, blank=True, help_text='Free text, e.g. "$1,500" or "Mechanical keyboard".')
    description = models.CharField(max_length=300, blank=True)
    quantity = models.PositiveSmallIntegerField(default=1, help_text="How many winners get this prize.")
    position = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["position", "id"]

    def __str__(self):
        return self.name


class Award(models.Model):
    """A prize given to a project. Winner certificates are made from these.

    Awards are public once the event's results date has passed (or straight
    away if it has none), so an organizer can decide winners before the
    ceremony without announcing them."""

    prize = models.ForeignKey(Prize, on_delete=models.CASCADE, related_name="awards")
    project = models.ForeignKey("projects.Project", on_delete=models.CASCADE, related_name="awards")
    note = models.CharField(max_length=200, blank=True, help_text="Optional. Why, in a line, for the certificate.")
    awarded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["prize__position", "created_at", "id"]
        constraints = [models.UniqueConstraint(fields=["prize", "project"], name="one_award_per_prize_per_project")]

    def __str__(self):
        return f"{self.prize} to {self.project}"


class CustomQuestion(models.Model):
    """An organizer-defined field on the submission form."""

    class Kind(models.TextChoices):
        SHORT = "short", "Short answer"
        LONG = "long", "Paragraph"
        URL = "url", "Link"
        CHOICE = "choice", "Pick one"
        YESNO = "yesno", "Yes or no"

    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="questions")
    prompt = models.CharField(max_length=240)
    help_text = models.CharField(max_length=300, blank=True)
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.SHORT)
    options = models.TextField(blank=True, help_text="For 'Pick one': one option per line.")
    required = models.BooleanField(default=False, help_text="Teams can save drafts without it, but not submit.")
    is_public = models.BooleanField(
        "show in gallery", default=True, help_text="Private answers are only seen by the team and organizers."
    )
    position = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["position", "id"]

    def __str__(self):
        return self.prompt

    @property
    def option_list(self):
        return [line.strip() for line in self.options.splitlines() if line.strip()]


class Activity(models.Model):
    """Append-only log of what happened in an event, with server timestamps.

    It is what an organizer reads when a team says "we definitely submitted
    before the deadline". Refused late edits are logged here too.
    """

    # No event means a platform-wide entry (sign-in lockouts, sign-up limits,
    # admin changes to accounts), shown on the admin page.
    event = models.ForeignKey(Event, null=True, blank=True, on_delete=models.CASCADE, related_name="activity")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    verb = models.CharField(max_length=40, db_index=True)
    detail = models.CharField(max_length=400, blank=True)
    team = models.ForeignKey("teams.Team", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    project = models.ForeignKey("projects.Project", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        verbose_name_plural = "activity"

    def __str__(self):
        return f"{self.actor}: {self.detail}"

    @property
    def is_refusal(self):
        return self.verb.endswith(".refused") or self.verb in REFUSAL_VERBS


REFUSAL_VERBS = {"rate.limited", "login.locked"}


def log_activity(event, actor, verb, detail="", team=None, project=None):
    """Write one audit line. Once the surrounding write commits, the line is
    also sent to any webhook that listens for it (webhooks/delivery.py)."""
    from webhooks.delivery import queue_after_commit

    entry = Activity.objects.create(
        event=event,
        actor=actor if getattr(actor, "is_authenticated", False) else None,
        verb=verb,
        detail=detail[:400],
        team=team,
        project=project,
    )
    queue_after_commit(entry)
    return entry


# Entries a team must never see in its own history: who judges their project,
# when a judge scored it, a judge's conflict and their reason, and anything
# about the community vote. They stay in the organizers' activity log.
STAFF_ONLY_VERBS = ("score.", "judge.", "judging.", "voting.")


def team_history(event, team, limit):
    entries = Activity.objects.filter(event=event, team=team)
    for prefix in STAFF_ONLY_VERBS:
        entries = entries.exclude(verb__startswith=prefix)
    return entries.select_related("actor")[:limit]
