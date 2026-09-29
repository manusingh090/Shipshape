import secrets

from django.conf import settings
from django.db import models
from django.urls import reverse
from django.utils import timezone


def new_invite_code():
    # 128 bits: not guessable, and short enough to paste into a chat.
    return secrets.token_urlsafe(16)


class Team(models.Model):
    event = models.ForeignKey("events.Event", on_delete=models.CASCADE, related_name="teams")
    # Names are not unique on purpose: the fixture data has two teams called
    # StillTrail, and real events do too. The id tells them apart.
    name = models.CharField(max_length=80)
    invite_code = models.CharField(max_length=40, unique=True, default=new_invite_code)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(default=timezone.now)
    external_id = models.CharField(max_length=40, blank=True, default="", db_index=True)

    class Meta:
        ordering = ["name", "id"]

    def __str__(self):
        return self.name

    def get_invite_url(self):
        return reverse("teams:join", args=[self.invite_code])

    @property
    def size(self):
        return self.memberships.count()

    @property
    def seats_left(self):
        return max(self.event.max_team_size - self.size, 0)

    @property
    def captain(self):
        m = self.memberships.filter(role=Membership.Role.CAPTAIN).select_related("user").first()
        return m.user if m else None


class Membership(models.Model):
    class Role(models.TextChoices):
        CAPTAIN = "captain", "Captain"
        MEMBER = "member", "Member"

    team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name="memberships")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="memberships")
    # Copied from team.event so the database itself can refuse a second team
    # for the same person in the same event.
    event = models.ForeignKey("events.Event", on_delete=models.CASCADE, related_name="+")
    role = models.CharField(max_length=10, choices=Role.choices, default=Role.MEMBER)
    joined_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["joined_at", "id"]
        constraints = [
            models.UniqueConstraint(fields=["event", "user"], name="one_team_per_person_per_event"),
        ]

    def __str__(self):
        return f"{self.user} in {self.team}"

    def save(self, *args, **kwargs):
        self.event_id = self.team.event_id
        super().save(*args, **kwargs)
