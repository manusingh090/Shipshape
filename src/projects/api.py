"""A small JSON API over the same services the web pages use.

CSRF: these endpoints are exempt from Django's token check because API
clients cannot fetch a token first. Instead a write must be
`Content-Type: application/json` (a cross-site HTML form cannot send that
without a CORS preflight, which this app never grants), any Origin header
must match this site, and the session cookie is SameSite=Lax. That is
enough to stop a malicious page from posting on a signed-in user's behalf.
"""

import json
from urllib.parse import urlsplit

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt

from events.access import Viewer
from events.deadline import WindowError, assert_accepting_edits
from events.models import Event
from events.timeutil import iso_utc

from . import services
from .forms import ProjectForm
from .images import ImageError
from .views import listed_projects


class ApiError(Exception):
    def __init__(self, status, code, detail, **extra):
        super().__init__(detail)
        self.status, self.code, self.detail, self.extra = status, code, detail, extra

    def response(self):
        return JsonResponse({"error": self.code, "detail": self.detail, **self.extra}, status=self.status)


def read_json(request):
    if request.content_type != "application/json":
        raise ApiError(415, "unsupported_media_type", "Send a JSON body with Content-Type: application/json.")
    origin = request.headers.get("Origin")
    if origin:
        parts = urlsplit(origin)
        if parts.netloc != request.get_host() or parts.scheme != request.scheme:
            raise ApiError(403, "cross_origin", "Requests from other sites are not accepted.")
    try:
        body = json.loads(request.body or b"{}")
    except (ValueError, UnicodeDecodeError):
        raise ApiError(400, "invalid_json", "The request body isn't valid JSON.")
    if not isinstance(body, dict):
        raise ApiError(400, "invalid_json", "Send a JSON object.")
    return body


def serialize(project, request, full=True):
    data = {
        "id": project.pk,
        "url": request.build_absolute_uri(project.get_absolute_url()),
        "event": project.event.slug,
        "title": project.title,
        "tagline": project.tagline,
        "team": {"id": project.team_id, "name": project.team.name},
        "track": {"id": project.track_id, "name": project.track.name} if project.track else None,
        "status": project.status,
        "submitted_at": iso_utc(project.submitted_at) if project.submitted_at else None,
        "updated_at": iso_utc(project.updated_at),
    }
    if full:
        data.update({
            "description": project.description,
            "repo_url": project.repo_url,
            "live_url": project.live_url,
            "demo_video_url": project.demo_video_url,
            "tags": list(project.tags.values_list("name", flat=True)),
            "answers": [
                {"question_id": a.question_id, "prompt": a.question.prompt, "value": a.value}
                for a in project.answers.select_related("question")
            ],
            "images": [request.build_absolute_uri(f"/media/{img.path}") for img in project.images.all()],
            "thumbnail": request.build_absolute_uri(f"/media/{project.thumbnail}") if project.thumbnail else None,
        })
    return data


def form_data(project, body):
    """Start from what is saved and lay the JSON fields over it, so a client
    can send only the fields it wants to change."""
    data = {}
    if project is not None:
        data = {
            "title": project.title,
            "tagline": project.tagline,
            "description": project.description,
            "track": project.track_id or "",
            "repo_url": project.repo_url,
            "live_url": project.live_url,
            "demo_video_url": project.demo_video_url,
            "tags": ", ".join(project.tags.values_list("name", flat=True)),
        }
        for answer in project.answers.all():
            data[f"q_{answer.question_id}"] = answer.value
    aliases = {"name": "title", "summary": "tagline", "repo": "repo_url", "video": "demo_video_url"}
    for key, value in body.items():
        key = aliases.get(key, key)
        if key in ("title", "tagline", "description", "repo_url", "live_url", "demo_video_url"):
            data[key] = "" if value is None else str(value)
        elif key == "track":
            data["track"] = "" if value is None else str(value)
        elif key == "tags":
            data["tags"] = ", ".join(map(str, value)) if isinstance(value, list) else str(value or "")
        elif key == "answers" and isinstance(value, dict):
            for qid, answer in value.items():
                data[f"q_{qid}"] = "" if answer is None else str(answer)
    return data


@csrf_exempt
def submission(request, slug):
    """GET or POST your team's submission for one event."""
    if request.method not in ("GET", "POST"):
        return ApiError(405, "method_not_allowed", "Use GET or POST.").response()
    if not request.user.is_authenticated:
        return ApiError(401, "not_signed_in", "Send a session cookie. Sign in at /login/ to get one.").response()
    event = Event.objects.filter(slug=slug).first()
    viewer = Viewer(request.user, event) if event else None
    if event is None or not viewer.can_view_event:
        return ApiError(404, "no_such_event", "No event with that address.").response()
    team = viewer.team

    if request.method == "GET":
        project = services.listed_project(team) if team else None
        if project is None:
            return ApiError(404, "no_project", "Your team hasn't saved a project for this event.").response()
        return JsonResponse(serialize(project, request))

    try:
        body = read_json(request)
        # The deadline comes before anything else about the payload: a late
        # request is refused as late, whatever it contains.
        try:
            assert_accepting_edits(event)
        except WindowError as exc:
            if team is not None:
                services.record_refusal(event, request.user, team, exc, "save the project", "API")
            raise ApiError(403, exc.code, exc.message, deadline=iso_utc(event.submissions_close_at))
        if team is None:
            raise ApiError(403, "no_team", "Join or start a team for this event before submitting.")

        action = body.get("action") or ("submit" if body.get("submit") is True else "save")
        if action not in ("save", "submit"):
            raise ApiError(400, "invalid_action", 'action must be "save" or "submit".')
        project = services.listed_project(team)
        enforce = action == "submit" or (project is not None and project.is_submitted)
        form = ProjectForm(data=form_data(project, body), event=event, project=project, enforce_required=enforce)
        if not form.is_valid():
            fields = {name: [e["message"] for e in errors] for name, errors in form.errors.get_json_data().items()}
            raise ApiError(400, "invalid", "Some fields need fixing.", fields=fields)
        try:
            project, created = services.save_submission(request.user, event, team, form, action=action, via="API")
        except WindowError as exc:
            raise ApiError(403, exc.code, exc.message, deadline=iso_utc(event.submissions_close_at))
        except services.SaveLimited as exc:
            raise ApiError(429, "slow_down", exc.message)
        except services.SubmissionError as exc:
            raise ApiError(400, "incomplete", exc.message, missing=exc.problems)
        except ImageError as exc:
            raise ApiError(400, "bad_image", str(exc))
    except ApiError as err:
        return err.response()
    return JsonResponse(serialize(project, request), status=201 if created else 200)


def project_list(request):
    """Public list of submitted projects. Same visibility rules as the gallery."""
    projects = listed_projects().select_related("event", "team", "track").order_by("id")
    event_slug = request.GET.get("event")
    if event_slug:
        projects = projects.filter(event__slug=event_slug)
    track = request.GET.get("track", "")
    if track.isdigit():
        projects = projects.filter(track_id=int(track))
    try:
        limit = min(max(int(request.GET.get("limit", 50)), 1), 200)
        offset = max(int(request.GET.get("offset", 0)), 0)
    except ValueError:
        return ApiError(400, "bad_paging", "limit and offset must be numbers.").response()
    total = projects.count()
    items = [serialize(p, request, full=False) for p in projects[offset:offset + limit]]
    return JsonResponse({"count": total, "limit": limit, "offset": offset, "results": items})
