import posixpath
import random
from pathlib import Path

from django.conf import settings
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.db.models.functions import Lower
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from accounts.decorators import login_required
from events.access import Viewer, can_view_project, get_event_for
from events.deadline import WindowError, assert_accepting_edits
from events.models import Event, Track, team_history

from . import comments, services
from .forms import ProjectForm
from .images import ImageError
from .models import Comment, Project, ProjectImage, Tag

PAGE_SIZE = 48
SORTS = {
    "title": ("A to Z", [Lower("title"), "id"]),
    "newest": ("Newest first", ["-submitted_at", "id"]),
    "oldest": ("Oldest first", ["submitted_at", "id"]),
    "shuffle": ("Shuffled", None),
}


def listed_projects(now=None):
    """What the public may see: submitted, not a flagged duplicate, in a
    published event whose gallery is open (the deadline has passed, unless
    the organizers show projects early)."""
    return Project.objects.filter(
        Q(event__gallery_before_deadline=True) | Q(event__submissions_close_at__lte=now or timezone.now()),
        status=Project.Status.SUBMITTED, duplicate_of__isnull=True, event__is_published=True,
    )


def search_projects(base, q="", track_id="", tag=""):
    """The gallery's search: every word must match the name, tagline,
    description, team, a tag or the track. Shared with the REST API."""
    projects = base
    for term in (q or "").split()[:6]:
        projects = projects.filter(
            Q(title__icontains=term) | Q(tagline__icontains=term) | Q(description__icontains=term)
            | Q(team__name__icontains=term) | Q(tags__name__icontains=term) | Q(track__name__icontains=term)
        )
    if str(track_id).isdigit():
        projects = projects.filter(track_id=int(track_id))
    if tag:
        projects = projects.filter(tags__name=tag)
    return projects.distinct().select_related("team", "track", "event")


def gallery(request, slug=None):
    """Public gallery. No sign-in needed; drafts and duplicates never appear."""
    event = None
    if slug:
        event, _ = get_event_for(request, slug)
    base = listed_projects()
    event_slug = ""
    if event is None:
        event_slug = request.GET.get("event", "").strip()
        if event_slug:
            event = Event.objects.filter(slug=event_slug, is_published=True).first()
            if event is None:
                event_slug = ""
    if event is not None:
        base = base.filter(event=event)

    q = request.GET.get("q", "").strip()[:100]
    track_id = request.GET.get("track", "").strip()
    tag = request.GET.get("tag", "").strip().lower()[:40]
    sort = request.GET.get("sort", "title")
    if sort not in SORTS:
        sort = "title"

    if not (track_id.isdigit() and event is not None):
        track_id = ""
    projects = search_projects(base, q, track_id, tag)

    seed = None
    if sort == "shuffle":
        seed = request.GET.get("seed", "")
        if not seed.isdigit():
            params = request.GET.copy()
            params["seed"] = str(random.randint(1000, 999999))
            params.pop("page", None)
            return redirect(f"{request.path}?{params.urlencode()}")
        ids = list(projects.values_list("id", flat=True))
        random.Random(int(seed)).shuffle(ids)
        order = {pk: i for i, pk in enumerate(ids)}
        object_list = sorted(projects, key=lambda p: order[p.pk])
    else:
        object_list = projects.order_by(*SORTS[sort][1])

    page = Paginator(object_list, PAGE_SIZE).get_page(request.GET.get("page"))

    tracks = []
    if event is not None:
        tracks = Track.objects.filter(event=event).annotate(
            n=Count("projects", filter=Q(projects__in=base))
        )
    tags = (
        Tag.objects.filter(projects__in=base)
        .annotate(n=Count("projects", distinct=True))
        .order_by("-n", "name")[:30]
    )
    event_options = Event.objects.filter(is_published=True, projects__in=listed_projects()).distinct() \
        if slug is None else []

    return render(request, "projects/gallery.html", {
        "event": event,
        "scoped": slug is not None,
        "event_slug": event_slug,
        "event_options": event_options,
        "page": page,
        "q": q,
        "track_id": track_id,
        "tag": tag,
        "sort": sort,
        "sorts": [(key, label) for key, (label, _) in SORTS.items()],
        "seed": seed,
        "tracks": tracks,
        "tags": tags,
        "filtered": bool(q or track_id or tag or event_slug),
        "show_event": event is None,
    })


def project_detail(request, pk):
    project = get_object_or_404(
        Project.objects.select_related("event", "team", "track", "duplicate_of"), pk=pk
    )
    viewer = Viewer(request.user, project.event)
    # 404 rather than 403: a draft's existence is nobody else's business.
    if not viewer.can_view_event or not can_view_project(request.user, project, viewer):
        raise Http404()
    is_team_member = viewer.team is not None and viewer.team.pk == project.team_id
    answers = project.answers.select_related("question")
    if not (is_team_member or viewer.is_organizer):
        answers = answers.filter(question__is_public=True)
    return render(request, "projects/detail.html", {
        "project": project,
        "event": project.event,
        "viewer": viewer,
        "is_team_member": is_team_member,
        "answers": answers,
        "images": project.images.all(),
        "tags": project.tags.all(),
        "members": project.team.memberships.select_related("user"),
        "comments": comments.visible_comments(project, viewer) if project.is_listed else [],
        "comment_block": comments.why_not(request.user, project, viewer),
        "comments_open": comments.thread_open(project),
        "comment_draft": request.session.pop("comment_draft", ""),
        "comment_max": comments.MAX_LENGTH,
    })


def _thread(project):
    return project.get_absolute_url() + "#comments"


@require_POST
@login_required
def comment_add(request, pk):
    project = get_object_or_404(Project.objects.select_related("event"), pk=pk)
    if not can_view_project(request.user, project):
        raise Http404()
    try:
        comments.add_comment(request.user, project, request.POST.get("body", ""))
    except comments.CommentError as err:
        messages.error(request, err.message)
        request.session["comment_draft"] = request.POST.get("body", "")[:comments.MAX_LENGTH]
    else:
        messages.success(request, "Comment posted. You can remove it any time with “Remove my comment”.")
    return redirect(_thread(project))


@require_POST
@login_required
def comment_remove(request, pk, comment_id):
    project = get_object_or_404(Project.objects.select_related("event"), pk=pk)
    if not can_view_project(request.user, project):
        raise Http404()
    comment = get_object_or_404(Comment.objects.select_related("author", "project__event", "project__team"),
                                pk=comment_id, project=project)
    try:
        comments.remove_comment(request.user, comment, request.POST.get("reason", ""))
    except comments.NotAllowed:
        raise PermissionDenied("Only the author or an organizer can remove a comment.")
    except comments.CommentError as err:
        messages.error(request, err.message)
    else:
        own = comment.author_id == request.user.pk
        messages.success(request, "Your comment was removed." if own else "Comment removed.")
    return redirect(_thread(project))


def _edit_context(event, viewer, team, project, form, **extra):
    context = {
        "event": event,
        "viewer": viewer,
        "team": team,
        "project": project,
        "form": form,
        "editable": event.is_open,
        "checklist": services.missing_items(project) if project else [],
        "images": project.images.all() if project else [],
        "max_images": settings.MAX_GALLERY_IMAGES,
        "history": (
            team_history(event, team, 12) if team else []
        ),
    }
    context["images_left"] = settings.MAX_GALLERY_IMAGES - len(context["images"])
    context.update(extra)
    return context


@login_required
def submission_edit(request, slug):
    event, viewer = get_event_for(request, slug)
    team = viewer.team
    if team is None:
        if viewer.is_staff:
            messages.info(request, "Event staff and platform admins don't submit projects.")
            return redirect("events:detail", slug=slug)
        messages.info(request, "Start a team (or join one with an invite link) before submitting.")
        return redirect("teams:mine", slug=slug)
    project = services.listed_project(team)

    if request.method != "POST":
        form = ProjectForm(event=event, project=project)
        return render(request, "projects/edit.html", _edit_context(event, viewer, team, project, form))

    action = request.POST.get("action", "save")
    if action not in ("save", "submit"):
        action = "save"
    # Refuse before even looking at the form, and say why. save_submission
    # checks again inside its transaction; this early check just means a late
    # save gets "closed" rather than a list of validation errors.
    try:
        assert_accepting_edits(event)
    except WindowError as exc:
        services.record_refusal(event, request.user, team, exc, "save the project", "web form")
        form = ProjectForm(event=event, project=project)
        return render(request, "projects/edit.html",
                      _edit_context(event, viewer, team, project, form, refused=exc.message), status=403)

    enforce = action == "submit" or (project is not None and project.is_submitted)
    form = ProjectForm(request.POST, request.FILES, event=event, project=project, enforce_required=enforce)
    if form.is_valid():
        try:
            project, created = services.save_submission(request.user, event, team, form, action=action)
        except WindowError as exc:
            form = ProjectForm(event=event, project=services.listed_project(team))
            return render(request, "projects/edit.html",
                          _edit_context(event, viewer, team, services.listed_project(team), form,
                                        refused=exc.message), status=403)
        except ImageError as exc:
            form.add_error("thumbnail", str(exc))
        except services.SubmissionError as exc:
            if not exc.problems:
                form.add_error(None, exc.message)
            for problem in exc.problems:
                form.add_error(None, f"Still missing: {problem}.")
        else:
            if action == "submit":
                messages.success(request, "Submitted. You can keep editing until the deadline.")
            elif created:
                messages.success(request, "Draft saved. It's private until you submit it.")
            else:
                messages.success(request, "Saved.")
            return redirect("projects:edit", slug=slug)
    return render(request, "projects/edit.html",
                  _edit_context(event, viewer, team, project, form), status=400)


@login_required
@require_POST
def submission_withdraw(request, slug):
    event, viewer = get_event_for(request, slug)
    if viewer.team is None:
        raise Http404()
    try:
        services.withdraw_submission(request.user, event, viewer.team)
    except (WindowError, services.SubmissionError) as exc:
        messages.error(request, exc.message)
    else:
        messages.success(request, "Moved back to draft. It's out of the gallery until you submit again.")
    return redirect("projects:edit", slug=slug)


@login_required
@require_POST
def image_upload(request, slug):
    event, viewer = get_event_for(request, slug)
    if viewer.team is None:
        raise Http404()
    files = request.FILES.getlist("images")
    if not files:
        messages.error(request, "Choose at least one image to upload.")
        return redirect("projects:edit", slug=slug)
    try:
        added, skipped = services.add_images(request.user, event, viewer.team, files)
    except (WindowError, services.SubmissionError) as exc:
        messages.error(request, exc.message)
    except ImageError as exc:
        messages.error(request, f"Nothing was uploaded: {exc}")
    else:
        note = f" {skipped} didn't fit (the limit is {settings.MAX_GALLERY_IMAGES})." if skipped else ""
        messages.success(request, f"Added {added} image{'s' if added != 1 else ''}.{note}")
    return redirect(reverse("projects:edit", args=[slug]) + "#images")


@login_required
@require_POST
def image_delete(request, slug, image_id):
    event, viewer = get_event_for(request, slug)
    if viewer.team is None:
        raise Http404()
    try:
        services.remove_image(request.user, event, viewer.team, image_id)
    except (WindowError, services.SubmissionError) as exc:
        messages.error(request, exc.message)
    else:
        messages.success(request, "Image removed.")
    return redirect(reverse("projects:edit", args=[slug]) + "#images")


CONTENT_TYPES = {".jpg": "image/jpeg", ".png": "image/png"}


def media(request, path):
    """Serve an uploaded image, but only to people who may see its project."""
    rel = posixpath.normpath(path).lstrip("/")
    if rel.startswith("..") or "\\" in rel or "\x00" in rel:
        raise Http404()
    suffix = Path(rel).suffix.lower()
    if suffix not in CONTENT_TYPES:
        raise Http404()
    root = Path(settings.MEDIA_ROOT).resolve()
    full = (root / rel).resolve()
    if root not in full.parents or not full.is_file():
        raise Http404()

    owner = Project.objects.select_related("event", "team").filter(thumbnail=rel).first()
    if owner is None:
        image = ProjectImage.objects.select_related("project__event", "project__team").filter(path=rel).first()
        owner = image.project if image else None
    if owner is None or not can_view_project(request.user, owner):
        raise Http404()

    response = FileResponse(open(full, "rb"), content_type=CONTENT_TYPES[suffix])
    response["Cache-Control"] = "private, max-age=3600"
    response["X-Content-Type-Options"] = "nosniff"
    response["Content-Security-Policy"] = "default-src 'none'; sandbox"
    return response
