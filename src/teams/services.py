"""Team rules, in one place so the web views and any future API agree.

* one team per person per event (also a database constraint);
* organizers and judges of an event can't be on a team in it;
* teams never grow past the event's max size;
* rosters freeze when submissions close.

Each function runs in a transaction and re-reads the event and team rows
before checking anything, so two people racing for the last seat can't
both win.
"""

from django.db import IntegrityError, transaction

from events.deadline import assert_team_changes_allowed, fresh
from events.models import EventRole, log_activity

from .models import Membership, Team, new_invite_code


class TeamError(Exception):
    pass


def _check_can_compete(user, event):
    if user.is_admin:
        raise TeamError("Platform admins can manage every event, so they can't compete in one.")
    if EventRole.objects.filter(event=event, user=user).exists():
        raise TeamError("You're on the staff of this event (organizer or judge), so you can't join a team in it.")
    existing = Membership.objects.filter(event=event, user=user).select_related("team").first()
    if existing:
        raise TeamError(f"You're already on {existing.team.name} for this event. Leave it first to switch teams.")


def create_team(user, event, name):
    name = (name or "").strip()
    if not name:
        raise TeamError("Give your team a name.")
    if len(name) > 80:
        raise TeamError("Keep the team name under 80 characters.")
    with transaction.atomic():
        event = fresh(event)
        assert_team_changes_allowed(event)
        _check_can_compete(user, event)
        team = Team.objects.create(event=event, name=name, created_by=user)
        try:
            Membership.objects.create(team=team, user=user, event=event, role=Membership.Role.CAPTAIN)
        except IntegrityError:
            raise TeamError("You're already on a team for this event.")
        log_activity(event, user, "team.created", f"started the team {team.name}", team=team)
    return team


def join_team(user, team):
    with transaction.atomic():
        event = fresh(team.event)
        team = Team.objects.select_for_update().get(pk=team.pk)
        assert_team_changes_allowed(event)
        if team.memberships.filter(user=user).exists():
            raise TeamError(f"You're already on {team.name}.")
        _check_can_compete(user, event)
        if team.memberships.count() >= event.max_team_size:
            raise TeamError(f"{team.name} is full ({event.max_team_size} people is the limit for {event.name}).")
        try:
            Membership.objects.create(team=team, user=user, event=event)
        except IntegrityError:
            raise TeamError("You're already on a team for this event.")
        log_activity(event, user, "team.joined", f"joined {team.name}", team=team)
    return team


def leave_team(user, team):
    """Leave a team. The longest-serving member becomes captain; the last
    person out dissolves the team and its project."""
    with transaction.atomic():
        event = fresh(team.event)
        team = Team.objects.select_for_update().get(pk=team.pk)
        assert_team_changes_allowed(event)
        membership = team.memberships.filter(user=user).first()
        if membership is None:
            raise TeamError("You're not on this team.")
        was_captain = membership.role == Membership.Role.CAPTAIN
        membership.delete()
        remaining = team.memberships.order_by("joined_at", "id")
        if not remaining.exists():
            name = team.name
            log_activity(event, user, "team.dissolved", f"left {name} as its last member, so the team was dissolved")
            team.delete()
            return None
        if was_captain:
            heir = remaining.first()
            heir.role = Membership.Role.CAPTAIN
            heir.save(update_fields=["role"])
        log_activity(event, user, "team.left", f"left {team.name}", team=team)
    return team


def remove_member(actor, team, member_user):
    with transaction.atomic():
        event = fresh(team.event)
        team = Team.objects.select_for_update().get(pk=team.pk)
        assert_team_changes_allowed(event)
        _require_captain(actor, team)
        if member_user.pk == actor.pk:
            raise TeamError("Use Leave team to remove yourself.")
        membership = team.memberships.filter(user=member_user).first()
        if membership is None:
            raise TeamError("That person isn't on this team.")
        membership.delete()
        log_activity(event, actor, "team.member_removed",
                     f"removed {member_user.display_name} from {team.name}", team=team)
    return team


def reset_invite(actor, team):
    with transaction.atomic():
        event = fresh(team.event)
        team = Team.objects.select_for_update().get(pk=team.pk)
        assert_team_changes_allowed(event)
        _require_captain(actor, team)
        team.invite_code = new_invite_code()
        team.save(update_fields=["invite_code"])
        log_activity(event, actor, "team.invite_reset",
                     f"made a new invite link for {team.name}; the old one stopped working", team=team)
    return team


def rename_team(actor, team, name):
    name = (name or "").strip()
    if not name or len(name) > 80:
        raise TeamError("Team names need 1 to 80 characters.")
    with transaction.atomic():
        event = fresh(team.event)
        team = Team.objects.select_for_update().get(pk=team.pk)
        assert_team_changes_allowed(event)
        _require_captain(actor, team)
        old = team.name
        team.name = name
        team.save(update_fields=["name"])
        log_activity(event, actor, "team.renamed", f"renamed {old} to {name}", team=team)
    return team


def _require_captain(user, team):
    if not team.memberships.filter(user=user, role=Membership.Role.CAPTAIN).exists():
        raise TeamError("Only the team captain can do that.")
