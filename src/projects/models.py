from django.conf import settings
from django.db import models
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone


class Tag(models.Model):
    name = models.CharField(max_length=40, unique=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Project(models.Model):
    """A team's submission to an event. One listed project per team per event.

    A second submission from the same team (the fixture has one) is kept but
    flagged with duplicate_of, which hides it from the gallery. The partial
    unique constraint below is what makes "one per team" hold even if a view
    forgets to check.
    """

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        SUBMITTED = "submitted", "Submitted"

    event = models.ForeignKey("events.Event", on_delete=models.CASCADE, related_name="projects")
    team = models.ForeignKey("teams.Team", on_delete=models.CASCADE, related_name="projects")
    track = models.ForeignKey(
        "events.Track", null=True, blank=True, on_delete=models.RESTRICT, related_name="projects"
    )
    title = models.CharField(max_length=120)
    tagline = models.CharField(max_length=200, blank=True)
    description = models.TextField(blank=True)
    thumbnail = models.CharField(max_length=255, blank=True)
    demo_video_url = models.URLField(max_length=500, blank=True)
    repo_url = models.URLField(max_length=500, blank=True)
    live_url = models.URLField(max_length=500, blank=True)
    tags = models.ManyToManyField(Tag, blank=True, related_name="projects")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)
    submitted_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(default=timezone.now)
    last_edited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    duplicate_of = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="duplicates"
    )
    external_id = models.CharField(max_length=40, blank=True, default="", db_index=True)

    class Meta:
        ordering = ["title", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["event", "team"],
                condition=Q(duplicate_of__isnull=True),
                name="one_listed_project_per_team",
            ),
        ]
        indexes = [models.Index(fields=["event", "status"], name="project_event_status")]

    def __str__(self):
        return self.title

    def get_absolute_url(self):
        return reverse("projects:detail", args=[self.pk])

    @property
    def is_submitted(self):
        return self.status == self.Status.SUBMITTED

    @property
    def is_listed(self):
        return self.is_submitted and self.duplicate_of_id is None


class ProjectImage(models.Model):
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="images")
    path = models.CharField(max_length=255)
    width = models.PositiveIntegerField(default=0)
    height = models.PositiveIntegerField(default=0)
    caption = models.CharField(max_length=140, blank=True)
    position = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["position", "id"]


class Answer(models.Model):
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="answers")
    question = models.ForeignKey("events.CustomQuestion", on_delete=models.CASCADE, related_name="answers")
    value = models.TextField(blank=True)

    class Meta:
        ordering = ["question__position", "question_id"]
        constraints = [
            models.UniqueConstraint(fields=["project", "question"], name="one_answer_per_question"),
        ]

    @property
    def display_value(self):
        if self.question.kind == "yesno":
            return {"yes": "Yes", "no": "No"}.get(self.value, self.value)
        return self.value


class Comment(models.Model):
    """A comment on a listed project. Anyone can read them; signed-in people
    who aren't judging the event can write them.

    Removal is soft: the row stays, so an organizer's decision and its reason
    are on record, and the thread shows that something was taken down.
    """

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="comments")
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="comments")
    body = models.TextField(max_length=2000)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    removed_at = models.DateTimeField(null=True, blank=True)
    removed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    removal_reason = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["created_at", "id"]
        indexes = [models.Index(fields=["project", "created_at"], name="comment_project_time")]

    def __str__(self):
        return f"Comment {self.pk} on {self.project_id}"

    @property
    def removed_by_author(self):
        return self.removed_at is not None and self.removed_by_id == self.author_id

