from django.contrib import messages
from django.contrib.auth.views import redirect_to_login
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from accounts.decorators import login_required
from accounts.models import User
from events.access import Viewer, get_event_for
from events.deadline import WindowError
from events.models import team_history

from . import services
from .models import Membership, Team


def _current_team(request, slug):
    event, viewer = get_event_for(request, slug)
    if viewer.team is None:
        messages.info(request, "You're not on a team for this event yet.")
        return event, viewer, None
    return event, viewer, viewer.team


@login_required
def my_team(request, slug):
    event, viewer = get_event_for(request, slug)
    if request.method == "POST":
        try:
            team = services.create_team(request.user, event, request.POST.get("name", ""))
        except (services.TeamError, WindowError) as exc:
            messages.error(request, str(exc))
            return redirect("teams:mine", slug=slug)
        messages.success(request, f"{team.name} is ready. Send the invite link below to your teammates.")
        return redirect("teams:mine", slug=slug)

    team = viewer.team
    if team is None:
        return render(request, "teams/start.html", {"event": event, "viewer": viewer})

    memberships = list(team.memberships.select_related("user").order_by("joined_at", "id"))
    project = team.projects.filter(duplicate_of__isnull=True).first()
    seats_left = max(event.max_team_size - len(memberships), 0)
    return render(request, "teams/team.html", {
        "event": event,
        "viewer": viewer,
        "team": team,
        "memberships": memberships,
        "is_captain": viewer.membership.role == Membership.Role.CAPTAIN,
        "invite_url": request.build_absolute_uri(team.get_invite_url()),
        "seats_left": seats_left,
        "empty_seats": range(seats_left),
        "project": project,
        "history": team_history(event, team, 15),
    })


def join(request, code):
    team = get_object_or_404(Team.objects.select_related("event"), invite_code=code)
    event = team.event
    if not request.user.is_authenticated:
        if request.method == "POST":
            return redirect_to_login(request.get_full_path())
        return render(request, "teams/join.html", {
            "team": team, "event": event, "members": team.memberships.select_related("user"),
            "viewer": None,
        })

    viewer = Viewer(request.user, event)
    if not viewer.can_view_event:
        return render(request, "errors/404.html", status=404)

    if request.method == "POST":
        try:
            services.join_team(request.user, team)
        except (services.TeamError, WindowError) as exc:
            messages.error(request, str(exc))
            return redirect("teams:join", code=code)
        messages.success(request, f"You're on {team.name} now.")
        return redirect("teams:mine", slug=event.slug)

    return render(request, "teams/join.html", {
        "team": team,
        "event": event,
        "viewer": viewer,
        "members": team.memberships.select_related("user"),
        "full": team.memberships.count() >= event.max_team_size,
    })


def _team_action(request, slug, action, success):
    event, viewer, team = _current_team(request, slug)
    if team is None:
        return redirect("teams:mine", slug=slug)
    try:
        result = action(team)
    except (services.TeamError, WindowError) as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, success(result))
    if request.POST.get("then") == "event":
        return redirect("events:detail", slug=slug)
    return redirect("teams:mine", slug=slug)


@login_required
@require_POST
def rename(request, slug):
    return _team_action(
        request, slug,
        lambda team: services.rename_team(request.user, team, request.POST.get("name", "")),
        lambda team: f"Renamed to {team.name}.",
    )


@login_required
@require_POST
def reset_invite(request, slug):
    return _team_action(
        request, slug,
        lambda team: services.reset_invite(request.user, team),
        lambda team: "New invite link made. The old link no longer works.",
    )


@login_required
@require_POST
def leave(request, slug):
    event, viewer, team = _current_team(request, slug)
    if team is None:
        return redirect("events:detail", slug=slug)
    try:
        remaining = services.leave_team(request.user, team)
    except (services.TeamError, WindowError) as exc:
        messages.error(request, str(exc))
        return redirect("teams:mine", slug=slug)
    if remaining is None:
        messages.success(request, f"You left {team.name}. You were the last member, so the team is gone.")
    else:
        messages.success(request, f"You left {team.name}.")
    return redirect("events:detail", slug=slug)


@login_required
@require_POST
def remove_member(request, slug, user_id):
    member = get_object_or_404(User, pk=user_id)
    return _team_action(
        request, slug,
        lambda team: services.remove_member(request.user, team, member),
        lambda team: f"{member.display_name} is off the team.",
    )
