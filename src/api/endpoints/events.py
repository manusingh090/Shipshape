"""Events and the organizer console: details, tracks, prizes, questions,
organizers, submissions, teams, the activity log."""

from datetime import datetime

from django.shortcuts import get_object_or_404

from events import audit, services
from events.access import Viewer, organizer_events, require_organizer, visible_events
from events.forms import EventCreateForm, EventForm, OrganizerForm, PrizeForm, QuestionForm, TrackForm
from events.models import CustomQuestion, Event, EventRole, Prize, Track
from projects.api import serialize as project_json
from teams.models import Team

from .. import schema as sc
from .. import serializers as s
from ..core import ApiError, endpoint, json_body, paginate, valid


# What each endpoint takes and returns (api/schema.py), for the OpenAPI document.
TRACKS_LIST = sc.array(sc.string(), "Track names, one per track. Add descriptions and prizes afterwards.")
EVENT_CREATE = sc.form_schema(EventCreateForm, drop=("tracks_text",), optional_fields=("timezone", "max_team_size"),
                              extra={"tracks": (TRACKS_LIST, False)})
EVENT_UPDATE = sc.form_schema(EventForm, partial=True)
CHILD_ITEM = {"tracks": sc.ref("Track"), "prizes": sc.ref("Prize"), "questions": sc.ref("Question")}
CHILD_BODY = {
    "tracks": lambda partial: sc.form_schema(TrackForm, partial=partial, event=True),
    "prizes": lambda partial: sc.form_schema(PrizeForm, partial=partial, event=True),
    "questions": lambda partial: sc.form_schema(QuestionForm, partial=partial, event=True, drop=("options",), extra={
        "options": (sc.any_of(sc.array(sc.string()), sc.string(), description="For kind=choice: the options, "
                              "as a list or one per line."), False)}),
}
ORGANIZER = sc.obj(id=sc.integer("The organizer role's id, for DELETE."), user=sc.ref("PersonWithEmail"))
SUBMISSION_ROW = sc.obj(**sc.COMPONENTS["ProjectSummary"]["properties"],
                        duplicate_of=sc.nullable(sc.integer("The project this one repeats, if it's a flagged duplicate.")))
ACTIVITY_QUERY = {"only": sc.enum("refused", "integrity", "judging", "voting", "submissions", "settings",
                                  description="One kind of entry."),
                  "q": sc.string("Search names, emails and text.")}

DATE_FIELDS = ("starts_at", "submissions_close_at", "judging_ends_at", "results_at", "opens_at", "closes_at")


def event_for(request, slug, organizer=False):
    event = Event.objects.filter(slug=slug).first()
    viewer = Viewer(request.user, event) if event else None
    if event is None or not viewer.can_view_event:
        raise ApiError(404, "no_such_event", "No event with that address.")
    if organizer:
        require_organizer(viewer)
    return event, viewer


def current(form):
    """A form's current values as form data, so a PATCH only sends changes."""
    data = {}
    for bound in form:
        value = bound.value()
        if hasattr(value, "all"):  # an m2m manager
            value = [obj.pk for obj in value.all()]
        elif isinstance(value, (list, tuple)):
            value = [getattr(v, "pk", v) for v in value]
        data[bound.name] = "" if value is None else value
    return data


def local_dates(body, tz):
    """Dates may come as the form's wall-clock time in the event's zone
    ("2026-03-01T18:00") or as ISO 8601 with an offset ("...Z"). The form
    parses the first kind, so convert the second."""
    out = dict(body)
    for name in DATE_FIELDS:
        if name not in out:
            continue
        value = out[name]
        if isinstance(value, str) and value:
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                continue
            if parsed.tzinfo is not None:
                out[name] = parsed.astimezone(tz).strftime("%Y-%m-%dT%H:%M")
        elif value is None:
            out[name] = ""
    return out


def _zone(name):
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    if not name:
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return None


def patch_form(form_class, instance, body, **kwargs):
    base = current(form_class(instance=instance, **kwargs))
    base.update(body)
    return valid(form_class(base, instance=instance, **kwargs))


# ------------------------------------------------------------------ events

@endpoint("GET", "events", who="anyone", auth=False, summary="Events you can see (published ones, plus your own drafts).",
          ui="Hackathons page",
          returns=sc.page(sc.ref("Event")))
def list_events(request):
    return paginate(request, visible_events(request.user).order_by("-submissions_close_at"),
                    lambda e: s.event(e, Viewer(request.user, e)))


@endpoint("POST", "events", who="platform organizer or admin", summary="Create an event. You become its organizer.",
          ui="Create an event", body='{"name": ..., "starts_at": ..., "submissions_close_at": ..., "tracks": ["..."]}',
          request=EVENT_CREATE, returns=sc.ref("Event"))
def create_event(request):
    if not request.user.can_create_events:
        raise ApiError(403, "forbidden", "Creating events needs the platform organizer role.")
    body = json_body(request)
    tz_name = body.get("timezone") or "UTC"
    tz = _zone(tz_name)
    if tz is None:
        raise ApiError(400, "invalid", "Unknown time zone.", fields={"timezone": ["Unknown time zone."]})
    data = {"timezone": tz_name, "max_team_size": 4, "is_published": False, "comments_enabled": True,
            **local_dates(body, tz)}
    if isinstance(data.get("tracks"), list):
        data["tracks_text"] = "\n".join(str(t) for t in data.pop("tracks"))
    event = services.create_event(request.user, valid(EventCreateForm(data)))
    return s.event(event, Viewer(request.user, event))


@endpoint("GET", "events/<slug:slug>", who="anyone", auth=False, summary="One event, with your roles in it.",
          ui="Event page",
          returns=sc.ref("Event"))
def get_event(request, slug):
    event, viewer = event_for(request, slug)
    return s.event(event, viewer)


@endpoint("PATCH", "events/<slug:slug>", who="organizer",
          summary="Change the details and dates, publish or unpublish, switch comments. Every moved date is logged.",
          ui="Console, Details and dates", body='{"submissions_close_at": "2026-03-01T18:00Z", "is_published": true}',
          request=EVENT_UPDATE, returns=sc.ref("Event"))
def update_event(request, slug):
    event, _ = event_for(request, slug, organizer=True)
    raw = json_body(request)
    body = local_dates(raw, _zone(raw.get("timezone")) or event.tzinfo)
    event = services.update_event(request.user, event, patch_form(EventForm, event, body))
    return s.event(event, Viewer(request.user, event))


@endpoint("GET", "organize", who="signed in", summary="The events you organize.", ui="Organize page",
          returns=sc.results(sc.ref("Event")))
def my_events(request):
    return {"results": [s.event(e) for e in organizer_events(request.user).order_by("-submissions_close_at")]}


# ------------------------------------------------ tracks, prizes, questions

CHILDREN = {
    "tracks": (Track, TrackForm, "track", s.track,
               "Projects are filed under that track. Move them to another track before deleting it."),
    "prizes": (Prize, PrizeForm, "prize", s.prize, ""),
    "questions": (CustomQuestion, QuestionForm, "question", s.question, ""),
}


def _children(kind):
    model, form_class, noun, dump, protect = CHILDREN[kind]
    ui = {"tracks": "Console, Tracks", "prizes": "Console, Prizes", "questions": "Console, Form questions"}[kind]

    @endpoint("GET", f"events/<slug:slug>/{kind}", who="anyone", auth=False, summary=f"The event's {kind}.", ui=ui,
              returns=sc.results(CHILD_ITEM[kind]))
    def list_view(request, slug):
        event, _ = event_for(request, slug)
        return {"results": [dump(o) for o in model.objects.filter(event=event)]}

    @endpoint("POST", f"events/<slug:slug>/{kind}", who="organizer", summary=f"Add a {noun}.", ui=f"{ui}, Add",
              request=CHILD_BODY[kind](False), returns=CHILD_ITEM[kind])
    def create_view(request, slug):
        event, _ = event_for(request, slug, organizer=True)
        body = json_body(request)
        if kind == "questions" and isinstance(body.get("options"), list):
            body["options"] = "\n".join(body["options"])
        blank = current(form_class(event=event))
        blank.update(body)
        return dump(services.save_child(request.user, event, noun, valid(form_class(blank, event=event))))

    @endpoint("PATCH", f"events/<slug:slug>/{kind}/<int:pk>", who="organizer", summary=f"Change a {noun}.",
              ui=f"{ui}, Edit", request=CHILD_BODY[kind](True), returns=CHILD_ITEM[kind])
    def update_view(request, slug, pk):
        event, _ = event_for(request, slug, organizer=True)
        obj = get_object_or_404(model, pk=pk, event=event)
        body = json_body(request)
        if kind == "questions" and isinstance(body.get("options"), list):
            body["options"] = "\n".join(body["options"])
        return dump(services.save_child(request.user, event, noun, patch_form(form_class, obj, body, event=event), obj))

    @endpoint("DELETE", f"events/<slug:slug>/{kind}/<int:pk>", who="organizer", summary=f"Delete a {noun}.",
              ui=f"{ui}, Delete", returns=sc.DELETED)
    def delete_view(request, slug, pk):
        event, _ = event_for(request, slug, organizer=True)
        obj = get_object_or_404(model, pk=pk, event=event)
        return {"deleted": services.delete_child(request.user, event, obj, noun, protect)}


for _kind in CHILDREN:
    _children(_kind)


# --------------------------------------------------------------- organizers

@endpoint("GET", "events/<slug:slug>/organizers", who="organizer", summary="The event's organizers.",
          ui="Console, Organizers",
          returns=sc.results(ORGANIZER))
def organizers(request, slug):
    event, _ = event_for(request, slug, organizer=True)
    roles = event.roles.filter(role=EventRole.Role.ORGANIZER).select_related("user")
    return {"results": [{"id": r.pk, "user": s.person(r.user, email=True)} for r in roles]}


@endpoint("POST", "events/<slug:slug>/organizers", who="organizer", summary="Add an organizer by email.",
          ui="Console, Organizers, Add", body='{"email": "..."}',
          request=sc.form_schema(OrganizerForm, event=True), returns=ORGANIZER)
def add_organizer(request, slug):
    event, _ = event_for(request, slug, organizer=True)
    form = valid(OrganizerForm(json_body(request), event=event))
    role = services.add_organizer(request.user, event, form.cleaned_data["user"])
    return {"id": role.pk, "user": s.person(role.user, email=True)}


@endpoint("DELETE", "events/<slug:slug>/organizers/<int:pk>", who="organizer",
          summary="Remove an organizer. The last one can't be removed.", ui="Console, Organizers, Remove",
          returns=sc.obj(removed=sc.string("The name of the organizer removed.")))
def remove_organizer(request, slug, pk):
    event, _ = event_for(request, slug, organizer=True)
    role = get_object_or_404(EventRole, pk=pk, event=event, role=EventRole.Role.ORGANIZER)
    return {"removed": services.remove_organizer(request.user, event, role)}


# ------------------------------------------------------ submissions, teams

@endpoint("GET", "events/<slug:slug>/submissions", who="organizer",
          summary="Every submission, drafts and duplicates included; ?status=submitted|draft|duplicate.",
          ui="Console, Submissions",
          returns=sc.page(SUBMISSION_ROW),
          query={"status": sc.enum("submitted", "draft", "duplicate", description="Only these.")})
def submissions(request, slug):
    event, _ = event_for(request, slug, organizer=True)
    projects = event.projects.select_related("team", "track", "duplicate_of", "event").order_by("title")
    status = request.GET.get("status", "")
    if status == "submitted":
        projects = projects.filter(status="submitted", duplicate_of__isnull=True)
    elif status == "draft":
        projects = projects.filter(status="draft", duplicate_of__isnull=True)
    elif status == "duplicate":
        projects = projects.filter(duplicate_of__isnull=False)
    return paginate(request, projects,
                    lambda p: {**project_json(p, request, full=False), "duplicate_of": p.duplicate_of_id})


@endpoint("POST", "events/<slug:slug>/submissions/<int:pk>/promote", who="organizer",
          summary="List a flagged duplicate instead of the submission it repeats.",
          ui="Console, Submissions, List this one instead",
          returns=sc.obj(listed=sc.integer("The project now listed."), hidden=sc.integer("The one now hidden.")))
def promote(request, slug, pk):
    event, _ = event_for(request, slug, organizer=True)
    listed, hidden = services.promote_duplicate(request.user, event, pk)
    return {"listed": listed.pk, "hidden": hidden.pk}


@endpoint("GET", "events/<slug:slug>/teams", who="organizer", summary="Every team and its members.",
          ui="Console, Teams",
          returns=sc.page(sc.ref("Team")))
def teams(request, slug):
    event, _ = event_for(request, slug, organizer=True)
    return paginate(request, Team.objects.filter(event=event).select_related("event").order_by("name"),
                    lambda t: s.team(t, request, with_invite=True))


@endpoint("GET", "events/<slug:slug>/activity", who="organizer",
          summary="The event's audit log, newest first; ?only=refused|integrity|judging|voting|submissions|settings, ?q=.",
          ui="Console, Activity",
          query=ACTIVITY_QUERY, returns=sc.page(sc.ref("Activity")))
def activity(request, slug):
    event, _ = event_for(request, slug, organizer=True)
    entries, _, _ = audit.filtered(event.activity.select_related("actor", "event"),
                                   request.GET.get("only", ""), request.GET.get("q", ""))
    return paginate(request, entries, s.activity)
