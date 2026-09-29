"""Saving submissions. The web form and the JSON API both end up here.

The deadline check happens inside the transaction, against a freshly read
event row, immediately before the write. A refused attempt is logged after
the transaction rolls back, so the refusal survives even though the edit
does not.
"""

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from events.deadline import SubmissionsClosed, WindowError, assert_accepting_edits, fresh
from events.models import log_activity
from events.timeutil import humanize_delta
from integrity import limits

from .images import delete_media, process_upload
from .models import Answer, Project, ProjectImage, Tag


class SubmissionError(Exception):
    def __init__(self, message, problems=None):
        super().__init__(message)
        self.message = message
        self.problems = problems or []


class SaveLimited(SubmissionError):
    status = 429


def listed_project(team):
    return team.projects.filter(duplicate_of__isnull=True).first()


def missing_items(project):
    """What stands between a project and being submittable, in plain words."""
    missing = []
    if not project.title.strip():
        missing.append("a project name")
    if not project.tagline.strip():
        missing.append("a one-line tagline")
    if not project.description.strip():
        missing.append("a description")
    if not project.track_id:
        missing.append("a track")
    answered = set()
    if project.pk:
        answered = set(project.answers.exclude(value="").values_list("question_id", flat=True))
    for question in project.event.questions.filter(required=True):
        if question.pk not in answered:
            missing.append(f"an answer to “{question.prompt}”")
    return missing


def record_refusal(event, user, team, exc, what, via):
    now = timezone.now()
    if isinstance(exc, SubmissionsClosed):
        late = humanize_delta(now - event.submissions_close_at)
        detail = f"tried to {what} ({via}) {late} after the deadline. Refused."
        verb = "late.refused"
    else:
        detail = f"tried to {what} ({via}) before kickoff. Refused."
        verb = "early.refused"
    log_activity(event, user, verb, detail, team=team)


def save_submission(user, event, team, form, action="save", via="web form"):
    """Create or update the team's project from a validated ProjectForm.

    action is "save" (keep the current status) or "submit" (make it visible
    to judges and the gallery). Returns (project, created).
    """
    # Checked before the transaction, so the refusal's audit line survives.
    limits.check("submission.save", user.pk, "That's a lot of saves in one minute. Wait a moment and try again.",
                 exc=SaveLimited, event=event, actor=user, detail=f"team {team.name}")
    limits.record("submission.save", user.pk)
    cleaned = form.cleaned_data
    upload = cleaned.get("thumbnail")
    new_thumb = None
    try:
        with transaction.atomic():
            event = fresh(event)
            assert_accepting_edits(event)
            if upload:
                new_thumb, _, _ = process_upload(upload, f"projects/{team.pk}")

            project = team.projects.select_for_update().filter(duplicate_of__isnull=True).first()
            created = project is None
            if created:
                project = Project(event=event, team=team)
            old_thumb = project.thumbnail

            project.title = cleaned["title"]
            project.tagline = cleaned.get("tagline", "").strip()
            project.description = cleaned.get("description", "").strip()
            project.track = cleaned.get("track")
            project.repo_url = cleaned.get("repo_url", "")
            project.live_url = cleaned.get("live_url", "")
            project.demo_video_url = cleaned.get("demo_video_url", "")
            if new_thumb:
                project.thumbnail = new_thumb
            elif cleaned.get("remove_thumbnail"):
                project.thumbnail = ""

            now = timezone.now()
            project.updated_at = now
            project.last_edited_by = user
            became_submitted = action == "submit" and project.status != Project.Status.SUBMITTED
            if became_submitted:
                project.status = Project.Status.SUBMITTED
                project.submitted_at = now
            if created:
                project.created_at = now
            project.save()

            project.tags.set([Tag.objects.get_or_create(name=name)[0] for name in cleaned.get("tags", [])])
            for question in form.questions:
                value = str(cleaned.get(form.question_key(question)) or "").strip()
                if value:
                    Answer.objects.update_or_create(project=project, question=question, defaults={"value": value})
                else:
                    Answer.objects.filter(project=project, question=question).delete()

            if project.status == Project.Status.SUBMITTED:
                problems = missing_items(project)
                if problems:
                    raise SubmissionError("This project isn't complete enough to be submitted.", problems)

            if created:
                log_activity(event, user, "project.created", f"started the project “{project.title}”",
                             team=team, project=project)
            if became_submitted:
                log_activity(event, user, "project.submitted", f"submitted “{project.title}” ({via})",
                             team=team, project=project)
            elif not created:
                log_activity(event, user, "project.saved", f"saved “{project.title}” ({via})",
                             team=team, project=project)
            if old_thumb and old_thumb != project.thumbnail:
                transaction.on_commit(lambda: delete_media(old_thumb))
    except WindowError as exc:
        delete_media(new_thumb)
        record_refusal(event, user, team, exc, "save the project", via)
        raise
    except Exception:
        delete_media(new_thumb)
        raise
    return project, created


def withdraw_submission(user, event, team, via="web form"):
    try:
        with transaction.atomic():
            event = fresh(event)
            assert_accepting_edits(event)
            project = team.projects.select_for_update().filter(duplicate_of__isnull=True).first()
            if project is None or project.status != Project.Status.SUBMITTED:
                raise SubmissionError("There's no submitted project to move back to draft.")
            project.status = Project.Status.DRAFT
            project.updated_at = timezone.now()
            project.last_edited_by = user
            project.save(update_fields=["status", "updated_at", "last_edited_by"])
            log_activity(event, user, "project.withdrawn", f"moved “{project.title}” back to draft",
                         team=team, project=project)
    except WindowError as exc:
        record_refusal(event, user, team, exc, "withdraw the project", via)
        raise
    return project


def add_images(user, event, team, files, via="web form"):
    saved = []
    try:
        with transaction.atomic():
            event = fresh(event)
            assert_accepting_edits(event)
            project = team.projects.select_for_update().filter(duplicate_of__isnull=True).first()
            if project is None:
                raise SubmissionError("Save your project once before adding images.")
            existing = project.images.count()
            room = settings.MAX_GALLERY_IMAGES - existing
            if room <= 0:
                raise SubmissionError(
                    f"You already have {settings.MAX_GALLERY_IMAGES} images. Remove one to add another."
                )
            for offset, upload in enumerate(files[:room]):
                path, width, height = process_upload(upload, f"projects/{team.pk}")
                saved.append(path)
                ProjectImage.objects.create(project=project, path=path, width=width, height=height,
                                            position=existing + offset + 1)
            project.updated_at = timezone.now()
            project.last_edited_by = user
            project.save(update_fields=["updated_at", "last_edited_by"])
            log_activity(event, user, "project.images",
                         f"added {len(saved)} image{'s' if len(saved) != 1 else ''} to “{project.title}”",
                         team=team, project=project)
    except WindowError as exc:
        for path in saved:
            delete_media(path)
        record_refusal(event, user, team, exc, "upload images", via)
        raise
    except Exception:
        for path in saved:
            delete_media(path)
        raise
    return len(saved), max(len(files) - len(saved), 0)


def remove_image(user, event, team, image_id, via="web form"):
    try:
        with transaction.atomic():
            event = fresh(event)
            assert_accepting_edits(event)
            image = ProjectImage.objects.select_related("project").filter(
                pk=image_id, project__team=team, project__duplicate_of__isnull=True
            ).first()
            if image is None:
                raise SubmissionError("That image isn't part of your project.")
            project = image.project
            image.delete()
            project.updated_at = timezone.now()
            project.last_edited_by = user
            project.save(update_fields=["updated_at", "last_edited_by"])
            log_activity(event, user, "project.images", f"removed an image from “{project.title}”",
                         team=team, project=project)
    except WindowError as exc:
        record_refusal(event, user, team, exc, "remove an image", via)
        raise
