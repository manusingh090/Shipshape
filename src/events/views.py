from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.http import Http404, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from accounts.decorators import event_creator_required, login_required
from projects.models import Project
from projects.views import listed_projects
from teams.models import Membership, Team
from voting.services import config_for as voting_config

from . import audit, services
from .access import get_event_for, organizer_events, require_organizer, visible_events
from .deadline import voting_opens_at, voting_phase
from .forms import EventCreateForm, EventForm, OrganizerForm, PrizeForm, QuestionForm, TrackForm
from .models import CustomQuestion, Event, EventRole, Prize, Track


def _with_counts(qs):
    return qs.annotate(
        team_count=Count("teams", distinct=True),
        project_count=Count(
            "projects",
            filter=Q(projects__status=Project.Status.SUBMITTED, projects__duplicate_of__isnull=True),
            distinct=True,
        ),
        track_count=Count("tracks", distinct=True),
    )


def home(request):
    now = timezone.now()
    events = list(_with_counts(visible_events(request.user)))
    running = sorted((e for e in events if e.phase(now) == Event.Phase.OPEN), key=lambda e: e.submissions_close_at)
    upcoming = sorted((e for e in events if e.phase(now) == Event.Phase.UPCOMING), key=lambda e: e.starts_at)
    finished = sorted(
        (e for e in events if e.phase(now) == Event.Phase.CLOSED),
        key=lambda e: e.submissions_close_at,
        reverse=True,
    )
    recent = (
        listed_projects()
        .select_related("team", "track", "event")
        .order_by("-submitted_at")[:4]
    )
    totals = {
        "events": len(events),
        "teams": sum(e.team_count for e in events),
        "projects": sum(e.project_count for e in events),
    }
    return render(request, "home.html", {
        "running": running,
        "upcoming": upcoming,
        "finished": finished,
        "recent": recent,
        "totals": totals,
    })


def _results_state(event):
    from judging import publishing

    return publishing.state(event)


def event_detail(request, slug):
    event, viewer = get_event_for(request, slug)
    tracks = event.tracks.annotate(
        project_count=Count(
            "projects",
            filter=Q(projects__status=Project.Status.SUBMITTED, projects__duplicate_of__isnull=True),
        )
    )
    project = None
    if viewer.team:
        project = viewer.team.projects.filter(duplicate_of__isnull=True).first()
    counts = {
        "teams": event.teams.count(),
        "participants": Membership.objects.filter(event=event).count(),
        "submitted": event.projects.filter(status=Project.Status.SUBMITTED, duplicate_of__isnull=True).count(),
        "judges": event.roles.filter(role=EventRole.Role.JUDGE).count(),
    }
    organizers = [r.user for r in event.roles.filter(role=EventRole.Role.ORGANIZER).select_related("user")]
    voting = None
    vote_config = voting_config(event)
    if vote_config is not None and vote_config.is_enabled:
        voting = {"config": vote_config, "phase": voting_phase(event, vote_config),
                  "opens_at": voting_opens_at(event, vote_config)}
    return render(request, "events/detail.html", {
        "voting": voting,
        "records": _records_panel(event),
        "results_state": _results_state(event),
        "event": event,
        "viewer": viewer,
        "tracks": tracks,
        "prizes": _prizes_with_winners(event),
        "questions": event.questions.all(),
        "milestones": event.milestones(),
        "project": project,
        "counts": counts,
        "organizers": organizers,
    })


def _records_panel(event):
    from records import signing
    from records.models import Record

    live = Record.objects.filter(event=event, revoked_at__isnull=True)
    if not live.exists():
        return None
    public = signing.public_key_hex()
    return {"count": live.count(), "judges": live.filter(kind=Record.Kind.JUDGE).count(),
            "key_id": signing.key_id(bytes.fromhex(public)), "public_key": public}


def _prizes_with_winners(event):
    """Prizes, each with the awards that are public yet (none before the
    results date)."""
    from records.services import public_awards

    won = {}
    for award in public_awards(event):
        won.setdefault(award.prize_id, []).append(award)
    prizes = list(event.prizes.select_related("track"))
    for prize in prizes:
        prize.public_awards = won.get(prize.pk, [])
    return prizes


@login_required
def organize(request):
    events = _with_counts(organizer_events(request.user)).order_by("-submissions_close_at")
    return render(request, "events/organize.html", {"events": events})


@event_creator_required
def event_create(request):
    form = EventCreateForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        event = services.create_event(request.user, form)
        state = "It's published." if event.is_published else "It's a draft until you publish it."
        messages.success(request, f"{event.name} is set up. {state}")
        return redirect("events:manage", slug=event.slug)
    return render(request, "events/create.html", {"form": form})


# ------------------------------------------------------------ organizer ----

def _organizer_context(request, slug):
    event, viewer = get_event_for(request, slug)
    require_organizer(viewer)
    return event, viewer


@login_required
def manage(request, slug):
    event, viewer = _organizer_context(request, slug)
    projects = event.projects.all()
    stats = {
        "teams": event.teams.count(),
        "participants": Membership.objects.filter(event=event).count(),
        "submitted": projects.filter(status=Project.Status.SUBMITTED, duplicate_of__isnull=True).count(),
        "drafts": projects.filter(status=Project.Status.DRAFT, duplicate_of__isnull=True).count(),
        "judges": event.roles.filter(role=EventRole.Role.JUDGE).count(),
        "teams_without_project": event.teams.exclude(projects__isnull=False).count(),
    }
    duplicates = projects.filter(duplicate_of__isnull=False).select_related("team", "duplicate_of")
    refused = event.activity.filter(verb__endswith=".refused")
    return render(request, "events/manage/overview.html", {
        "event": event,
        "viewer": viewer,
        "section": "overview",
        "stats": stats,
        "duplicates": duplicates,
        "refused_count": refused.count(),
        "recent": event.activity.select_related("actor")[:12],
        "milestones": event.milestones(),
    })


@login_required
def manage_details(request, slug):
    event, viewer = _organizer_context(request, slug)
    form = EventForm(request.POST or None, instance=event)
    if request.method == "POST" and form.is_valid():
        try:
            event = services.update_event(request.user, event, form)
        except services.EventError as err:
            form.add_error("submissions_close_at", err.message)
        else:
            messages.success(request, "Event details saved.")
            return redirect("events:manage_details", slug=event.slug)
        event.refresh_from_db()
    return render(request, "events/manage/details.html", {
        "event": event, "viewer": viewer, "section": "details", "form": form,
    })


def _crud(request, slug, *, model, form_class, template, section, noun, protect_message=""):
    """List, add, edit and delete one kind of child object of an event."""
    event, viewer = _organizer_context(request, slug)
    editing = None
    if request.method == "POST":
        op = request.POST.get("op", "save")
        if op == "delete":
            obj = get_object_or_404(model, pk=request.POST.get("id"), event=event)
            try:
                label = services.delete_child(request.user, event, obj, noun, protect_message)
            except services.EventError as err:
                messages.error(request, err.message)
            else:
                messages.success(request, f"Deleted “{label}”.")
            return redirect(request.path)
        if op != "save":
            return HttpResponseBadRequest("Unknown operation.")
        pk = request.POST.get("id")
        editing = get_object_or_404(model, pk=pk, event=event) if pk else None
        form = form_class(request.POST, instance=editing, event=event)
        if form.is_valid():
            obj = services.save_child(request.user, event, noun, form, editing)
            messages.success(request, f"{'Saved' if editing else 'Added'} “{obj}”.")
            return redirect(request.path)
    else:
        edit_id = request.GET.get("edit")
        if edit_id:
            editing = get_object_or_404(model, pk=edit_id, event=event)
        form = form_class(instance=editing, event=event)
    return render(request, template, {
        "event": event,
        "viewer": viewer,
        "section": section,
        "form": form,
        "editing": editing,
        "items": model.objects.filter(event=event),
    })


@login_required
def manage_tracks(request, slug):
    return _crud(
        request, slug, model=Track, form_class=TrackForm, template="events/manage/tracks.html",
        section="tracks", noun="track",
        protect_message="Projects are filed under that track. Move them to another track before deleting it.",
    )


@login_required
def manage_prizes(request, slug):
    return _crud(request, slug, model=Prize, form_class=PrizeForm, template="events/manage/prizes.html",
                 section="prizes", noun="prize")


@login_required
def manage_questions(request, slug):
    return _crud(request, slug, model=CustomQuestion, form_class=QuestionForm,
                 template="events/manage/questions.html", section="questions", noun="question")


@login_required
def manage_people(request, slug):
    event, viewer = _organizer_context(request, slug)
    form = OrganizerForm(request.POST or None, event=event)
    if request.method == "POST" and form.is_valid():
        user = form.cleaned_data["user"]
        services.add_organizer(request.user, event, user)
        messages.success(request, f"{user.display_name} now helps organize {event.name}.")
        return redirect("events:manage_people", slug=slug)
    return render(request, "events/manage/people.html", {
        "event": event,
        "viewer": viewer,
        "section": "people",
        "form": form,
        "organizers": event.roles.filter(role=EventRole.Role.ORGANIZER).select_related("user").order_by("user__name"),
        "judge_count": event.roles.filter(role=EventRole.Role.JUDGE).count(),
    })


@login_required
@require_POST
def manage_people_remove(request, slug, pk):
    event, viewer = _organizer_context(request, slug)
    # Judges are removed under Judging, which also clears their unfinished reviews.
    staff = get_object_or_404(EventRole, pk=pk, event=event, role=EventRole.Role.ORGANIZER)
    try:
        name = services.remove_organizer(request.user, event, staff)
    except services.EventError as err:
        messages.error(request, err.message)
        return redirect("events:manage_people", slug=slug)
    messages.success(request, f"{name} is no longer a {staff.role} here.")
    if staff.user_id == request.user.pk and not request.user.is_admin:
        return redirect("events:detail", slug=slug)
    return redirect("events:manage_people", slug=slug)


@login_required
def manage_submissions(request, slug):
    event, viewer = _organizer_context(request, slug)
    status = request.GET.get("status", "")
    projects = event.projects.select_related("team", "track", "duplicate_of").order_by("title")
    if status == "submitted":
        projects = projects.filter(status=Project.Status.SUBMITTED, duplicate_of__isnull=True)
    elif status == "draft":
        projects = projects.filter(status=Project.Status.DRAFT, duplicate_of__isnull=True)
    elif status == "duplicate":
        projects = projects.filter(duplicate_of__isnull=False)
    return render(request, "events/manage/submissions.html", {
        "event": event, "viewer": viewer, "section": "submissions",
        "projects": projects, "status": status,
    })


@login_required
def manage_teams(request, slug):
    event, viewer = _organizer_context(request, slug)
    teams = (
        Team.objects.filter(event=event)
        .prefetch_related("memberships__user", "projects")
        .order_by("name")
    )
    return render(request, "events/manage/teams.html", {
        "event": event, "viewer": viewer, "section": "teams", "teams": teams,
    })


@login_required
def manage_activity(request, slug):
    event, viewer = _organizer_context(request, slug)
    items, only, q = audit.filtered(event.activity.select_related("actor", "project", "team"),
                                    request.GET.get("only", ""), request.GET.get("q", ""))
    page = Paginator(items, 50).get_page(request.GET.get("page"))
    return render(request, "events/manage/activity.html", {
        "event": event, "viewer": viewer, "section": "activity", "page": page, "only": only, "q": q,
        "categories": audit.category_links([k for k, _, _ in audit.CATEGORIES if k != "accounts"]),
    })


@login_required
@require_POST
def promote_duplicate(request, slug, pk):
    """Swap which of two submissions from the same team is the listed one."""
    event, viewer = _organizer_context(request, slug)
    try:
        duplicate, original = services.promote_duplicate(request.user, event, pk)
    except services.EventError:
        raise Http404()
    messages.success(request, f"#{duplicate.pk} is now the listed submission; #{original.pk} is hidden as its duplicate.")
    return redirect("events:manage_submissions", slug=slug)
