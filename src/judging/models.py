"""Judging data.

The assignment table is the only thing that lets a judge near a project: a
score can only exist for an assignment (one-to-one), and every judge-facing
query starts from "assignments where judge = the signed-in user".
"""

import json
import secrets
from datetime import timedelta

from django.conf import settings
from django.db import models
from django.db.models import F, Q
from django.utils import timezone


class JudgingConfig(models.Model):
    """Per-event judging settings. Created on first use."""

    event = models.OneToOneField("events.Event", on_delete=models.CASCADE, related_name="judging_config")
    scale_min = models.PositiveSmallIntegerField("lowest mark", default=1)
    scale_max = models.PositiveSmallIntegerField("highest mark", default=5)
    reviews_per_project = models.PositiveSmallIntegerField(
        "reviews per project", default=3,
        help_text="How many judges should score each project. Assignment batches fill up to this.",
    )
    kappa = models.FloatField(
        "normalization strength (kappa)", default=5.0,
        help_text="How many projects' worth of population spread every judge starts with. 5 is the tested default.",
    )
    # Publishing the judges' results (judging/publishing.py). Nothing derived
    # is stored: the published ranking is computed per request, like the
    # organizers' one. Publishing ends judging, so it can't change after.
    results_published_at = models.DateTimeField(null=True, blank=True)
    results_published_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    share_feedback = models.BooleanField(
        "share the judges' feedback with each team", default=False,
        help_text="Each team sees its own average marks per criterion and the judges' comments, never who wrote them.",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(scale_max__gt=F("scale_min")), name="judging_scale_is_a_range"),
            models.CheckConstraint(condition=Q(reviews_per_project__gte=1), name="judging_needs_a_review"),
            models.CheckConstraint(condition=Q(kappa__gte=0), name="judging_kappa_not_negative"),
        ]

    def __str__(self):
        return f"Judging settings for {self.event}"


class Criterion(models.Model):
    """One line of the organizer's rubric. Every criterion shares the event's
    scale, so a weighted sum stays on that scale and normalization compares
    like with like."""

    event = models.ForeignKey("events.Event", on_delete=models.CASCADE, related_name="criteria")
    key = models.SlugField(max_length=40, help_text="Short name used in CSV columns and the API.")
    label = models.CharField(max_length=80)
    description = models.CharField(
        max_length=300, blank=True, help_text="What judges should look for. Shown next to the marks."
    )
    weight = models.DecimalField(
        max_digits=5, decimal_places=2, default=1,
        help_text="Relative importance. Weights don't need to add up to anything.",
    )
    position = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["position", "id"]
        constraints = [
            models.UniqueConstraint(fields=["event", "key"], name="criterion_key_unique_per_event"),
            models.CheckConstraint(condition=Q(weight__gt=0), name="criterion_weight_positive"),
        ]

    def __str__(self):
        return self.label


def new_invite_token():
    return secrets.token_urlsafe(24)


def default_invite_expiry():
    return timezone.now() + timedelta(days=14)


class JudgeInvite(models.Model):
    """A single-use link that makes whoever accepts it a judge in one event.

    There is no mail server on an offline laptop, so the organizer copies the
    link and sends it however they like, the same way team invites work.
    """

    event = models.ForeignKey("events.Event", on_delete=models.CASCADE, related_name="judge_invites")
    token = models.CharField(max_length=48, unique=True, default=new_invite_token)
    email = models.EmailField(blank=True, help_text="If given, only the account with this email can accept.")
    tracks = models.ManyToManyField("events.Track", blank=True, related_name="+")
    all_tracks = models.BooleanField(default=False)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField(default=default_invite_expiry)
    accepted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    accepted_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Judge invite for {self.event} ({self.email or 'anyone'})"

    @property
    def state(self):
        if self.accepted_at:
            return "accepted"
        if self.revoked_at:
            return "revoked"
        if timezone.now() >= self.expires_at:
            return "expired"
        return "open"


class AssignmentBatch(models.Model):
    """One run of the assignment engine, or one import. The seed makes a run
    reproducible: same inputs, same seed, same assignments."""

    class Mode(models.TextChoices):
        ALGORITHMIC = "algorithmic", "Algorithmic"
        IMPORT = "import", "Imported"

    event = models.ForeignKey("events.Event", on_delete=models.CASCADE, related_name="assignment_batches")
    label = models.CharField(max_length=80)
    mode = models.CharField(max_length=12, choices=Mode.choices, default=Mode.ALGORITHMIC)
    scope = models.CharField(max_length=200, blank=True)
    seed = models.BigIntegerField(null=True, blank=True)
    target_reviews = models.PositiveSmallIntegerField(default=3)
    shortfall_count = models.PositiveIntegerField(default=0)
    shortfall_detail = models.TextField(blank=True, help_text="JSON: [[project id, reviews missing], ...]")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["created_at", "id"]
        verbose_name_plural = "assignment batches"

    def __str__(self):
        return self.label

    @property
    def shortfalls(self):
        return json.loads(self.shortfall_detail or "[]")


class Assignment(models.Model):
    event = models.ForeignKey("events.Event", on_delete=models.CASCADE, related_name="assignments")
    judge = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="judging_assignments")
    project = models.ForeignKey("projects.Project", on_delete=models.CASCADE, related_name="assignments")
    batch = models.ForeignKey(AssignmentBatch, null=True, blank=True, on_delete=models.SET_NULL,
                              related_name="assignments")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["created_at", "id"]
        constraints = [
            models.UniqueConstraint(fields=["judge", "project"], name="one_assignment_per_judge_and_project"),
        ]
        indexes = [models.Index(fields=["event", "judge"], name="assignment_event_judge")]

    def __str__(self):
        return f"{self.judge} reviews {self.project}"


class Conflict(models.Model):
    """A judge who must never review a project: declared by the judge or set by
    an organizer. The engine treats these like the tracks rule."""

    event = models.ForeignKey("events.Event", on_delete=models.CASCADE, related_name="judge_conflicts")
    judge = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="judging_conflicts")
    project = models.ForeignKey("projects.Project", on_delete=models.CASCADE, related_name="judge_conflicts")
    reason = models.CharField(max_length=200)
    declared_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["judge", "project"], name="one_conflict_row_per_judge_and_project"),
        ]


class Score(models.Model):
    """A judge's marks for one assigned project. Drafts have no submitted_at
    and never count; a second submission updates this row (and leaves a
    ScoreRevision behind) rather than creating a second ballot."""

    class Source(models.TextChoices):
        JUDGE = "judge", "Judge"
        IMPORT = "import", "Imported"

    assignment = models.OneToOneField(Assignment, on_delete=models.CASCADE, related_name="score")
    event = models.ForeignKey("events.Event", on_delete=models.CASCADE, related_name="judge_scores")
    judge = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="judging_scores")
    project = models.ForeignKey("projects.Project", on_delete=models.CASCADE, related_name="judge_scores")
    comment = models.TextField(blank=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    source = models.CharField(max_length=8, choices=Source.choices, default=Source.JUDGE)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["judge", "project"], name="one_score_per_judge_and_project"),
        ]
        indexes = [models.Index(fields=["event", "submitted_at"], name="score_event_submitted")]

    def __str__(self):
        return f"{self.judge} on {self.project}"

    @property
    def is_submitted(self):
        return self.submitted_at is not None


class CriterionScore(models.Model):
    score = models.ForeignKey(Score, on_delete=models.CASCADE, related_name="items")
    criterion = models.ForeignKey(Criterion, on_delete=models.CASCADE, related_name="+")
    value = models.PositiveSmallIntegerField()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["score", "criterion"], name="one_mark_per_criterion"),
        ]


class ScoreRevision(models.Model):
    """Append-only history: every save of a score writes one of these, so
    "who changed what, when" never depends on anyone's memory."""

    event = models.ForeignKey("events.Event", on_delete=models.CASCADE, related_name="+")
    judge = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    project = models.ForeignKey("projects.Project", on_delete=models.CASCADE, related_name="+")
    score = models.ForeignKey(Score, null=True, on_delete=models.SET_NULL, related_name="revisions")
    values_json = models.TextField(help_text="JSON: {criterion key: mark}")
    comment = models.TextField(blank=True)
    submitted = models.BooleanField(default=False)
    source = models.CharField(max_length=8, default=Score.Source.JUDGE)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    @property
    def values(self):
        return json.loads(self.values_json or "{}")
