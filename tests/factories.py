import io
from datetime import timedelta

from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from PIL import Image

from accounts.models import User
from events.models import CustomQuestion, Event, EventRole, Track
from teams.models import Membership, Team

PASSWORD = "correct horse battery"


def make_user(email, name=None, role=User.Role.MEMBER):
    return User.objects.create_user(
        email=email, name=name or email.split("@")[0].title(), password=PASSWORD, platform_role=role
    )


def make_event(slug="test-hack", phase="open", **overrides):
    now = timezone.now()
    starts, closes = {
        "open": (now - timedelta(days=1), now + timedelta(days=1)),
        "closed": (now - timedelta(days=3), now - timedelta(hours=1)),
        "upcoming": (now + timedelta(days=1), now + timedelta(days=3)),
    }[phase]
    fields = {
        "name": slug.replace("-", " ").title(),
        "starts_at": starts,
        "submissions_close_at": closes,
        "is_published": True,
        "max_team_size": 3,
    }
    fields.update(overrides)
    event = Event.objects.create(slug=slug, **fields)
    Track.objects.create(event=event, name="Tools", position=1)
    return event


def make_team(event, captain, *members, name="Night Owls"):
    team = Team.objects.create(event=event, name=name)
    Membership.objects.create(team=team, user=captain, event=event, role=Membership.Role.CAPTAIN)
    for member in members:
        Membership.objects.create(team=team, user=member, event=event)
    return team


def add_staff(event, user, role=EventRole.Role.ORGANIZER):
    return EventRole.objects.create(event=event, user=user, role=role)


def add_question(event, prompt="Which APIs did you use?", required=True, public=True, kind="long"):
    return CustomQuestion.objects.create(event=event, prompt=prompt, required=required, is_public=public,
                                         kind=kind, position=1)


def complete_submission(event, **extra):
    """POST data for a submission that satisfies every built-in rule."""
    data = {
        "title": "Quiet Hours",
        "tagline": "Mutes your notifications while you focus.",
        "track": str(event.tracks.first().pk),
        "description": "A small tool that does one thing.",
        "repo_url": "https://example.org/repo",
        "tags": "Django, SQLite",
    }
    data.update(extra)
    return data


def png_upload(name="shot.png", size=(64, 48), colour=(200, 80, 40)):
    buffer = io.BytesIO()
    Image.new("RGB", size, colour).save(buffer, "PNG")
    return SimpleUploadedFile(name, buffer.getvalue(), content_type="image/png")
