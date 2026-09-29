"""Judges: adding them, inviting them, and changing what they may review."""

from datetime import timedelta

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from accounts.models import normalize_email
from events.models import EventRole, log_activity
from teams.models import Membership

from .errors import JudgingError
from .models import Assignment, Conflict, JudgeInvite, JudgingConfig, Score


def config_for(event):
    config, _ = JudgingConfig.objects.get_or_create(event=event)
    return config


def scoring_started(event):
    return Score.objects.filter(event=event, submitted_at__isnull=False).exists()


def describe_tracks(role_or_invite):
    if role_or_invite.all_tracks:
        return "every track"
    names = [t.name for t in role_or_invite.tracks.all()]
    return ", ".join(names) if names else "no tracks yet"


def check_can_judge(user, event):
    """Anyone who can see every score (organizers, admins) can't also judge,
    and nobody competing in an event can judge it."""
    name = user.display_name
    if not user.is_active:
        raise JudgingError("That account is deactivated.")
    if user.is_admin:
        raise JudgingError(f"{name} is a platform admin and can see every score, so they can't also judge.")
    if EventRole.objects.filter(event=event, user=user, role=EventRole.Role.ORGANIZER).exists():
        raise JudgingError(f"{name} organizes this event and can see every score, so they can't also judge it.")
    if Membership.objects.filter(event=event, user=user).exists():
        raise JudgingError(f"{name} is on a team in this event. Staff can't compete in their own event.")
    if EventRole.objects.filter(event=event, user=user, role=EventRole.Role.JUDGE).exists():
        raise JudgingError(f"{name} already judges this event.")


def add_judge(actor, event, user, tracks, all_tracks=False):
    with transaction.atomic():
        check_can_judge(user, event)
        role = EventRole.objects.create(event=event, user=user, role=EventRole.Role.JUDGE, added_by=actor,
                                        all_tracks=all_tracks)
        role.tracks.set(tracks)
        log_activity(event, actor, "judge.added", f"added {user.display_name} as a judge for {describe_tracks(role)}")
    return role


def _pending(queryset):
    """Assignments without a submitted score (untouched or only a draft)."""
    return queryset.filter(Q(score__isnull=True) | Q(score__submitted_at__isnull=True))


def update_judge_tracks(actor, role, tracks, all_tracks):
    """Change what a judge may review. Unfinished assignments outside the new
    tracks are removed, so a judge's queue never shows another track."""
    with transaction.atomic():
        role.all_tracks = all_tracks
        role.save(update_fields=["all_tracks"])
        role.tracks.set(tracks)
        removed = 0
        if not all_tracks:
            allowed = [t.pk for t in tracks]
            stale = _pending(Assignment.objects.filter(event=role.event, judge=role.user)).exclude(
                project__track_id__in=allowed
            )
            removed = stale.count()
            stale.delete()
        note = f"; {removed} unfinished review{'s' if removed != 1 else ''} outside those tracks removed" if removed else ""
        log_activity(role.event, actor, "judge.tracks",
                     f"set {role.user.display_name} to judge {describe_tracks(role)}{note}")
    return removed


def remove_judge(actor, role):
    """Stop someone judging. Their unfinished assignments go; scores they
    already submitted stay in the record and keep counting."""
    with transaction.atomic():
        pending = _pending(Assignment.objects.filter(event=role.event, judge=role.user))
        removed = pending.count()
        pending.delete()
        kept = Score.objects.filter(event=role.event, judge=role.user, submitted_at__isnull=False).count()
        name = role.user.display_name
        event = role.event
        role.delete()
        log_activity(event, actor, "judge.removed",
                     f"removed {name} as a judge ({removed} unfinished review{'s' if removed != 1 else ''} dropped, "
                     f"{kept} submitted score{'s' if kept != 1 else ''} kept)")
    return removed, kept


def add_conflict(actor, event, judge_user, project, reason):
    """Record that a judge must never review a project. Refused if they
    already submitted a score for it: that's for an organizer to settle."""
    with transaction.atomic():
        assignment = Assignment.objects.filter(event=event, judge=judge_user, project=project).first()
        if assignment is not None:
            if Score.objects.filter(assignment=assignment, submitted_at__isnull=False).exists():
                if actor == judge_user:
                    raise JudgingError(
                        f"You already submitted a score for “{project.title}”. Tell an organizer about "
                        "the conflict so they can decide what to do with it."
                    )
                raise JudgingError(
                    f"{judge_user.display_name} already submitted a score for “{project.title}”. "
                    "Scores aren't deleted; decide separately whether to disregard it."
                )
            assignment.delete()
        Conflict.objects.update_or_create(
            event=event, judge=judge_user, project=project,
            defaults={"reason": reason[:200], "declared_by": actor},
        )
        who = "declared a conflict" if actor == judge_user else f"recorded a conflict for {judge_user.display_name}"
        log_activity(event, actor, "judge.conflict", f"{who} with “{project.title}”: {reason[:120]}",
                     team=project.team, project=project)


# ----------------------------------------------------------------- invites ----

def create_invite(actor, event, email, tracks, all_tracks, days=14):
    with transaction.atomic():
        invite = JudgeInvite.objects.create(
            event=event, email=normalize_email(email) if email else "", all_tracks=all_tracks,
            created_by=actor, expires_at=timezone.now() + timedelta(days=days),
        )
        invite.tracks.set(tracks)
        log_activity(event, actor, "judge.invited",
                     f"made a judge invite link for {invite.email or 'anyone with the link'} ({describe_tracks(invite)})")
    return invite


def revoke_invite(actor, invite):
    if invite.state != "open":
        raise JudgingError("That invite is no longer open.")
    invite.revoked_at = timezone.now()
    invite.save(update_fields=["revoked_at"])
    log_activity(invite.event, actor, "judge.invite_revoked",
                 f"revoked the judge invite for {invite.email or 'anyone with the link'}")


def accept_invite(user, invite):
    with transaction.atomic():
        invite = JudgeInvite.objects.select_for_update().select_related("event").get(pk=invite.pk)
        state = invite.state
        if state == "accepted":
            raise JudgingError("This invite has already been used.")
        if state == "revoked":
            raise JudgingError("The organizers withdrew this invite.")
        if state == "expired":
            raise JudgingError("This invite has expired. Ask the organizers for a new link.")
        if invite.email and invite.email != user.email:
            raise JudgingError(
                f"This invite is for {invite.email}. Sign in with that account, or ask the organizers for a new link."
            )
        check_can_judge(user, invite.event)
        role = EventRole.objects.create(event=invite.event, user=user, role=EventRole.Role.JUDGE,
                                        added_by=invite.created_by, all_tracks=invite.all_tracks)
        role.tracks.set(invite.tracks.all())
        invite.accepted_by = user
        invite.accepted_at = timezone.now()
        invite.save(update_fields=["accepted_by", "accepted_at"])
        log_activity(invite.event, user, "judge.joined",
                     f"accepted a judge invite and now judges {describe_tracks(role)}")
    return role


# ------------------------------------------------------------- the rubric ----

def save_config(actor, event, form):
    """form: a valid ConfigForm for this event's JudgingConfig."""
    changed = ", ".join(form.fields[name].label.lower() for name in form.changed_data)
    config = form.save()
    if changed:
        log_activity(event, actor, "judging.config", f"changed the judging settings ({changed})")
    return config


def save_criterion(actor, event, form, editing=None):
    """form: a valid CriterionForm. New criteria can't be added once scoring
    has started (the form already fixes the key); weights can always move."""
    if editing is None and scoring_started(event):
        raise JudgingError("Scoring has started, so the rubric can't gain criteria. Weights can still change.")
    # From the database: validating the form already put the new weight on editing.
    old_weight = type(editing).objects.get(pk=editing.pk).weight if editing else None
    criterion = form.save(commit=False)
    criterion.event = event
    criterion.save()
    if editing and old_weight != criterion.weight:
        detail = f"changed the weight of “{criterion.label}” from {old_weight} to {criterion.weight}"
    else:
        detail = f"{'updated' if editing else 'added'} the criterion “{criterion.label}”"
    log_activity(event, actor, "judging.rubric", detail)
    return criterion


def delete_criterion(actor, event, criterion):
    if scoring_started(event):
        raise JudgingError("Scoring has started, so criteria can't be removed. Lower its weight instead.")
    label = criterion.label
    criterion.delete()
    log_activity(event, actor, "judging.rubric", f"removed the criterion “{label}”")
    return label

