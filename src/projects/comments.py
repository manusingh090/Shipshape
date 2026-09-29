"""Comments on gallery projects. Every rule lives here.

* Anyone can read the comments on a project they can see. Only listed
  projects (submitted, not a flagged duplicate, published event) have a
  thread, so drafts never do.
* Writing needs an account, and the event must have comments switched on.
* Judges of the event can't comment on its projects: a judge's public
  opinion of a project they might be scoring would tell other judges and the
  team what they think, which is exactly what role isolation keeps private.
* Authors can remove their own comments, and they vanish from the thread
  for everyone; organizers can remove anyone's, with a reason that goes in
  the activity log. Removal is soft: every row stays in the CSV export.
* Rate limits: 5 comments a minute and 30 an hour per person, and the same
  text can't be posted twice in a row on one project.

Comments never feed into judging or the community vote.
"""

from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from events.access import Viewer
from events.models import log_activity
from integrity import limits

from .models import Comment

MAX_LENGTH = 2000


class CommentError(Exception):
    status = 400
    code = "comment_error"

    def __init__(self, message):
        super().__init__(message)
        self.message = message


class NotAllowed(CommentError):
    status = 403
    code = "not_allowed"


class SlowDown(CommentError):
    status = 429
    code = "slow_down"


def thread_open(project):
    return (project.is_listed and project.event.is_published and project.event.gallery_open()
            and project.event.comments_enabled)


def why_not(user, project, viewer=None):
    """The reason this person can't comment here, or None if they can."""
    if not project.is_listed or not project.event.is_published or not project.event.gallery_open():
        return "Only projects in the gallery have comments."
    if not project.event.comments_enabled:
        return "The organizers have turned comments off for this event."
    if not user.is_authenticated:
        return "Sign in to comment."
    viewer = viewer or Viewer(user, project.event)
    if viewer.is_judge:
        return ("You're judging this event, so you can't comment on its projects: "
                "your opinion would reach the other judges and the teams.")
    return None


def visible_comments(project, viewer):
    """Comments in order. A comment its author removed is gone for everyone
    (it stays in the organizers' CSV export). One an organizer removed shows
    as a placeholder, and organizers see its text and the reason."""
    rows = list(project.comments.select_related("author", "removed_by"))
    team_ids = set(project.team.memberships.values_list("user_id", flat=True))
    organizer_ids = set(project.event.roles.filter(role="organizer").values_list("user_id", flat=True))
    out = []
    for c in rows:
        if c.removed_by_author:
            continue
        out.append({
            "comment": c,
            "hidden": c.removed_at is not None and not viewer.is_organizer,
            "badge": "Team" if c.author_id in team_ids else ("Organizer" if c.author_id in organizer_ids else ""),
            "mine": viewer.signed_in and c.author_id == viewer.user.pk,
        })
    return out


def add_comment(user, project, body):
    body = (body or "").strip()
    # Rate limits first, outside the transaction, so a refusal is logged.
    if user.is_authenticated:
        now = timezone.now()
        mine = Comment.objects.filter(author=user)
        limits.check("comment.minute", user.pk, "That's a lot of comments in a minute. Wait a moment.",
                     count=mine.filter(created_at__gte=now - timedelta(minutes=1)).count(), exc=SlowDown,
                     event=project.event, actor=user)
        limits.check("comment.hour", user.pk, "You've hit the limit of 30 comments an hour. Try again later.",
                     count=mine.filter(created_at__gte=now - timedelta(hours=1)).count(), exc=SlowDown,
                     event=project.event, actor=user)
    with transaction.atomic():
        # Re-read the project and event, like every other write.
        project = type(project).objects.select_related("event").select_for_update().get(pk=project.pk)
        reason = why_not(user, project)
        if reason:
            raise NotAllowed(reason)
        if not body:
            raise CommentError("Write something first.")
        if len(body) > MAX_LENGTH:
            raise CommentError(f"Keep it under {MAX_LENGTH} characters ({len(body)} now).")
        now = timezone.now()
        mine = Comment.objects.filter(author=user)
        last = mine.filter(project=project).order_by("-created_at").first()
        if last and last.removed_at is None and last.body == body:
            raise CommentError("You've just posted exactly that.")
        comment = Comment.objects.create(project=project, author=user, body=body, created_at=now)
        log_activity(project.event, user, "comment.posted", f"commented on “{project.title}”",
                     team=project.team, project=project)
        return comment


def remove_comment(user, comment, reason=""):
    viewer = Viewer(user, comment.project.event)
    own = comment.author_id == user.pk
    if not (own or viewer.is_organizer):
        raise NotAllowed("Only the author or an organizer can remove a comment.")
    if comment.removed_at:
        return comment
    reason = (reason or "").strip()[:200]
    if not own and not reason:
        raise CommentError("Say briefly why, for example “personal attack”. It goes in the activity log.")
    comment.removed_at = timezone.now()
    comment.removed_by = user
    comment.removal_reason = "" if own else reason
    comment.save(update_fields=["removed_at", "removed_by", "removal_reason"])
    project = comment.project
    if own:
        # So a webhook mirroring the thread knows to take it down too.
        log_activity(project.event, user, "comment.deleted", f"removed their own comment on “{project.title}”",
                     team=project.team, project=project)
    else:
        log_activity(project.event, user, "comment.removed",
                     f"removed a comment by {comment.author.display_name} on “{project.title}”: {reason}",
                     team=project.team, project=project)
    return comment
