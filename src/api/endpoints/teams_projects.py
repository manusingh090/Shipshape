"""Teams, submissions, images, the gallery and comments."""

import random

from django.db.models.functions import Lower
from django.http import Http404
from django.shortcuts import get_object_or_404

from accounts.models import User
from events.access import Viewer, can_view_project
from projects import api as submission_api
from projects import comments, services as project_services
from projects.forms import ProjectForm
from projects.models import Comment, Project
from projects.views import listed_projects, search_projects
from teams import services as team_services
from teams.models import Team

from .. import schema as sc
from .. import serializers as s
from ..core import ApiError, endpoint, json_body, paginate
from .events import event_for


# What each endpoint takes and returns (api/schema.py), for the OpenAPI document.
TEAM_NAME = sc.obj(name=sc.string(maxLength=80))
MY_TEAM = sc.obj(**sc.COMPONENTS["Team"]["properties"], project=sc.nullable(sc.integer("Your team's project, if saved.")),
                 _description="Your team. invite_code and invite_url are included.")
MY_TEAM["required"] = sc.COMPONENTS["Team"]["required"] + ["project"]
INVITE = sc.obj(**sc.COMPONENTS["Team"]["properties"], full=sc.boolean("Whether the team has room."))
INVITE["required"] = sc.COMPONENTS["Team"]["required"] + ["full"]
SUBMISSION_BODY = sc.obj(
    "Send only the fields that change; the rest keep their saved values. The deadline is checked before anything "
    "else, so a late request is refused as late (403 submissions_closed) whatever it contains.",
    action=sc.optional(sc.enum("save", "submit", description="save keeps a draft (or updates a submitted project); "
                                                             "submit checks every required field and lists it.")),
    submit=sc.optional(sc.boolean("The same as action=submit.")),
    title=sc.optional(sc.string(maxLength=120)), name=sc.optional(sc.string("Another name for title.")),
    tagline=sc.optional(sc.string()), summary=sc.optional(sc.string("Another name for tagline.")),
    description=sc.optional(sc.string("Markdown.")), track=sc.optional(sc.nullable(sc.integer("A track id."))),
    repo_url=sc.optional(sc.string(format="uri")), repo=sc.optional(sc.string("Another name for repo_url.")),
    live_url=sc.optional(sc.string(format="uri")), demo_video_url=sc.optional(sc.string(format="uri")),
    video=sc.optional(sc.string("Another name for demo_video_url.")),
    tags=sc.optional(sc.any_of(sc.array(sc.string()), sc.string(), description="Tech tags, a list or comma separated.")),
    answers=sc.optional(sc.mapping(sc.nullable(sc.string()), "Answers to the form's questions, by question id.")))
THUMBNAIL_UPLOAD = sc.obj(thumbnail=sc.string("A PNG, JPEG, GIF or WebP; re-encoded and stripped of metadata.",
                                              format="binary"))
IMAGES_UPLOAD = sc.obj(images=sc.array(sc.string(format="binary"), "One or more images, up to 8 per project."))
ADDED_IMAGES = sc.obj(added=sc.integer(), skipped=sc.integer("Files not added: over the limit of 8, or not images."),
                      project=sc.ref("Project"))
GALLERY_QUERY = {"q": sc.string("Every word must match the name, tagline, description, team, a tag or the track."),
                 "event": sc.string("An event's slug."), "track": sc.integer("A track id."), "tag": sc.string(),
                 "sort": sc.enum("title", "newest", "oldest", "shuffle"),
                 "seed": sc.integer("With sort=shuffle: the same seed gives the same order on every page.")}
COMMENTS = sc.obj(results=sc.array(sc.ref("Comment")), can_comment=sc.boolean(),
                  why_not=sc.nullable(sc.string("Why you can't comment here, if you can't.")))


def _my_team(request, slug):
    event, viewer = event_for(request, slug)
    if viewer.team is None:
        raise ApiError(404, "no_team", "You're not on a team for this event.")
    return event, viewer, viewer.team


# -------------------------------------------------------------------- teams

@endpoint("GET", "events/<slug:slug>/team", who="participant", summary="Your team, with its invite link.",
          ui="Team page",
          returns=MY_TEAM)
def my_team(request, slug):
    event, viewer, team = _my_team(request, slug)
    project = project_services.listed_project(team)
    return {**s.team(team, request, with_invite=True), "project": project.pk if project else None}


@endpoint("POST", "events/<slug:slug>/team", who="signed in, not on a team or staff",
          summary="Start a team. You're its captain.", ui="Start a team", body='{"name": "..."}',
          request=TEAM_NAME, returns=sc.ref("Team"))
def create_team(request, slug):
    event, _ = event_for(request, slug)
    team = team_services.create_team(request.user, event, json_body(request).get("name", ""))
    return s.team(team, request, with_invite=True)


@endpoint("PATCH", "events/<slug:slug>/team", who="captain", summary="Rename your team.", ui="Team page, Rename",
          body='{"name": "..."}',
          request=TEAM_NAME, returns=sc.ref("Team"))
def rename_team(request, slug):
    _, _, team = _my_team(request, slug)
    team = team_services.rename_team(request.user, team, json_body(request).get("name", ""))
    return s.team(team, request, with_invite=True)


@endpoint("POST", "events/<slug:slug>/team/invite/reset", who="captain",
          summary="Make a new invite link; the old one stops working.", ui="Team page, New invite link",
          returns=sc.ref("Team"))
def reset_invite(request, slug):
    _, _, team = _my_team(request, slug)
    team = team_services.reset_invite(request.user, team)
    return s.team(team, request, with_invite=True)


@endpoint("POST", "events/<slug:slug>/team/leave", who="participant",
          summary="Leave your team. The last person out dissolves it.", ui="Team page, Leave",
          returns=sc.obj(left=sc.string("The team's name."), team_dissolved=sc.boolean()))
def leave_team(request, slug):
    _, _, team = _my_team(request, slug)
    remaining = team_services.leave_team(request.user, team)
    return {"left": team.name, "team_dissolved": remaining is None}


@endpoint("DELETE", "events/<slug:slug>/team/members/<int:user_id>", who="captain",
          summary="Take someone off your team.", ui="Team page, Remove",
          returns=sc.ref("Team"))
def remove_member(request, slug, user_id):
    _, _, team = _my_team(request, slug)
    member = get_object_or_404(User, pk=user_id)
    team_services.remove_member(request.user, team, member)
    return s.team(Team.objects.get(pk=team.pk), request, with_invite=True)


@endpoint("GET", "invites/<str:code>", who="signed in", summary="The team an invite link is for.",
          ui="Join page",
          returns=INVITE)
def invite_preview(request, code):
    team = get_object_or_404(Team.objects.select_related("event"), invite_code=code)
    return {**s.team(team), "full": team.memberships.count() >= team.event.max_team_size}


@endpoint("POST", "invites/<str:code>/join", who="signed in", summary="Join the team an invite link is for.",
          ui="Join page, Join the team",
          returns=sc.ref("Team"))
def join_team(request, code):
    team = get_object_or_404(Team.objects.select_related("event"), invite_code=code)
    team_services.join_team(request.user, team)
    return s.team(team)


# -------------------------------------------------------------- submission

def _delegate(view):
    """The older /api/events/<slug>/submission view already does exactly what
    the form does, with the deadline checked first. v1 hands it the request
    (with the token's user already in place) rather than copying it."""
    def run(request, slug):
        return view(request, slug)
    return run


_submission = _delegate(submission_api.submission)


@endpoint("GET", "events/<slug:slug>/submission", who="participant", summary="Your team's project, drafts too.",
          ui="Submission form",
          returns=sc.ref("Project"))
def get_submission(request, slug):
    return _submission(request, slug)


@endpoint("POST", "events/<slug:slug>/submission", who="participant",
          summary="Save or submit your team's project. Send only the fields that change. Refused after the deadline.",
          ui="Submission form, Save / Submit",
          body='{"action": "save|submit", "title": ..., "tagline": ..., "track": 3, "tags": [...], "answers": {"7": "..."}}',
          request=SUBMISSION_BODY, returns=sc.ref("Project"), created=True)
def save_submission(request, slug):
    return _submission(request, slug)


@endpoint("POST", "events/<slug:slug>/submission/withdraw", who="participant",
          summary="Take a submitted project back to draft (before the deadline).", ui="Submission form, Back to draft",
          returns=sc.ref("Project"))
def withdraw(request, slug):
    event, _, team = _my_team(request, slug)
    project_services.withdraw_submission(request.user, event, team, via="API")
    return submission_api.serialize(project_services.listed_project(team), request)


@endpoint("POST", "events/<slug:slug>/submission/thumbnail", who="participant (API token)", upload=True,
          summary="Upload the thumbnail: multipart, field `thumbnail`. Checked and re-encoded like the form's.",
          ui="Submission form, Thumbnail",
          multipart=THUMBNAIL_UPLOAD, returns=sc.ref("Project"))
def thumbnail(request, slug):
    event, _, team = _my_team(request, slug)
    project = project_services.listed_project(team)
    if project is None:
        raise ApiError(404, "no_project", "Save the project once before adding a thumbnail.")
    upload = request.FILES.get("thumbnail")
    if upload is None:
        raise ApiError(400, "invalid", "Send the image as multipart field `thumbnail`.")
    form = ProjectForm(data=submission_api.form_data(project, {}), files={"thumbnail": upload}, event=event,
                       project=project, enforce_required=project.is_submitted)
    if not form.is_valid():
        from ..core import form_errors
        raise form_errors(form)
    project, _ = project_services.save_submission(request.user, event, team, form, action="save", via="API")
    return submission_api.serialize(project, request)


@endpoint("POST", "events/<slug:slug>/submission/images", who="participant (API token)", upload=True,
          summary="Add gallery images: multipart, one or more `images` fields. Up to 8 per project.",
          ui="Submission form, Add images",
          multipart=IMAGES_UPLOAD, returns=ADDED_IMAGES)
def add_images(request, slug):
    event, _, team = _my_team(request, slug)
    added, skipped = project_services.add_images(request.user, event, team, request.FILES.getlist("images"), via="API")
    return {"added": added, "skipped": skipped,
            "project": submission_api.serialize(project_services.listed_project(team), request)}


@endpoint("DELETE", "events/<slug:slug>/submission/images/<int:image_id>", who="participant",
          summary="Remove a gallery image.", ui="Submission form, Remove image",
          returns=sc.ref("Project"))
def remove_image(request, slug, image_id):
    event, _, team = _my_team(request, slug)
    project_services.remove_image(request.user, event, team, image_id, via="API")
    return submission_api.serialize(project_services.listed_project(team), request)


# ------------------------------------------------------------------ gallery

GALLERY_SORTS = {"title": [Lower("title"), "id"], "newest": ["-submitted_at", "id"], "oldest": ["submitted_at", "id"]}


@endpoint("GET", "projects", who="anyone", auth=False,
          summary="The gallery. ?q= searches, ?event=, ?track=, ?tag= filter, ?sort=title|newest|oldest|shuffle&seed=.",
          ui="Gallery",
          query=GALLERY_QUERY, returns=sc.page(sc.ref("ProjectSummary")))
def gallery(request):
    base = listed_projects()
    slug = request.GET.get("event", "")
    if slug:
        base = base.filter(event__slug=slug)
    projects = search_projects(base, request.GET.get("q", "")[:100], request.GET.get("track", ""),
                               request.GET.get("tag", "").strip().lower()[:40])
    sort = request.GET.get("sort", "title")
    if sort == "shuffle":
        seed = request.GET.get("seed", "")
        if not seed.isdigit():
            raise ApiError(400, "invalid", "A shuffle needs ?seed=<number>, so the pages agree with each other.")
        ordered = list(projects)
        random.Random(int(seed)).shuffle(ordered)
    else:
        ordered = projects.order_by(*GALLERY_SORTS.get(sort, GALLERY_SORTS["title"]))
    return paginate(request, ordered, lambda p: submission_api.serialize(p, request, full=False))


def _visible_project(request, pk):
    project = get_object_or_404(Project.objects.select_related("event", "team", "track"), pk=pk)
    if not can_view_project(request.user, project):
        raise Http404  # the same answer as a project that doesn't exist, so ids reveal nothing
    return project


@endpoint("GET", "projects/<int:pk>", who="anyone", auth=False,
          summary="One project. Drafts and duplicates only for their team and the organizers.", ui="Project page",
          returns=sc.ref("Project"))
def project(request, pk):
    p = _visible_project(request, pk)
    viewer = Viewer(request.user, p.event)
    data = submission_api.serialize(p, request)
    own = viewer.team is not None and viewer.team.pk == p.team_id
    if not (own or viewer.is_organizer):
        data["answers"] = [a for a, obj in zip(data["answers"], p.answers.select_related("question"))
                           if obj.question.is_public]
    return data


# ----------------------------------------------------------------- comments

@endpoint("GET", "projects/<int:pk>/comments", who="anyone", auth=False, summary="A project's comments.",
          ui="Project page, Comments",
          returns=COMMENTS)
def list_comments(request, pk):
    p = _visible_project(request, pk)
    viewer = Viewer(request.user, p.event)
    rows = comments.visible_comments(p, viewer) if p.is_listed else []
    return {"results": [s.comment(r) for r in rows], "can_comment": comments.why_not(request.user, p, viewer) is None,
            "why_not": comments.why_not(request.user, p, viewer)}


@endpoint("POST", "projects/<int:pk>/comments", who="signed in, not judging the event",
          summary="Comment on a listed project. Safe Markdown; rate limited.", ui="Project page, Post comment",
          body='{"body": "..."}',
          request=sc.obj(body=sc.string("Markdown, up to 2000 characters.", maxLength=2000)),
          returns=sc.ref("Comment"))
def add_comment(request, pk):
    p = _visible_project(request, pk)
    c = comments.add_comment(request.user, p, json_body(request).get("body", ""))
    return s.comment({"comment": c, "hidden": False, "badge": "", "mine": True})


@endpoint("DELETE", "projects/<int:pk>/comments/<int:comment_id>", who="author, or organizer with a reason",
          summary="Remove a comment. Organizers must say why.", ui="Project page, Remove my comment / Remove",
          body='{"reason": "..."} (organizers)',
          request=sc.obj(reason=sc.optional(sc.string("Organizers removing someone else's: why."))),
          returns=sc.obj(removed=sc.boolean()))
def remove_comment(request, pk, comment_id):
    p = _visible_project(request, pk)
    c = get_object_or_404(Comment.objects.select_related("author", "project__event", "project__team"),
                          pk=comment_id, project=p)
    try:
        comments.remove_comment(request.user, c, json_body(request).get("reason", ""))
    except comments.NotAllowed as err:
        raise ApiError(403, "forbidden", err.message)
    return {"removed": True}
