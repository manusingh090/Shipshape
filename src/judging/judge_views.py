"""The judge's side: their events, their queue, their scoring form.

Every lookup goes through judging/access.py, which only ever returns rows
for the signed-in judge. Anything else is a 404, never a 403, so a judge
can't learn which project ids exist or which track they belong to.
"""

from django.contrib import messages
from django.contrib.auth.views import redirect_to_login
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from accounts.decorators import login_required
from events.deadline import WindowError, judging_phase
from events.models import Event

from .access import judge_role, judged_events, my_assignment, my_assignments
from .errors import JudgingError, NotAssigned
from .models import JudgeInvite, Score
from .scoring import declare_conflict, rubric_for, save_score, weighted
from .staff import accept_invite, config_for, describe_tracks


def _status(score):
    if score is None:
        return "todo"
    return "submitted" if score.submitted_at else "draft"


def _queue(user, event):
    assignments = list(my_assignments(user, event))
    scores = {s.assignment_id: s for s in Score.objects.filter(assignment__in=assignments).prefetch_related("items")}
    _, rubric = rubric_for(event)
    rows = []
    for a in assignments:
        score = scores.get(a.pk)
        total = weighted(score, rubric) if score and score.submitted_at else None
        rows.append({"assignment": a, "project": a.project, "score": score, "status": _status(score), "total": total})
    order = {"todo": 0, "draft": 1, "submitted": 2}
    rows.sort(key=lambda r: (order[r["status"]], r["project"].title))
    return rows


@login_required
def home(request):
    events = []
    for event in judged_events(request.user).order_by("-submissions_close_at"):
        rows = _queue(request.user, event)
        events.append({
            "event": event,
            "total": len(rows),
            "done": sum(1 for r in rows if r["status"] == "submitted"),
            "phase": judging_phase(event),
            "role": judge_role(request.user, event),
        })
    return render(request, "judging/judge_home.html", {"events": events})


def _judge_event(request, slug):
    event = get_object_or_404(Event, slug=slug)
    role = judge_role(request.user, event)
    if role is None:
        raise Http404()
    return event, role


@login_required
def queue(request, slug):
    event, role = _judge_event(request, slug)
    rows = _queue(request.user, event)
    return render(request, "judging/judge_queue.html", {
        "event": event, "role": role, "rows": rows, "tracks": describe_tracks(role),
        "done": sum(1 for r in rows if r["status"] == "submitted"), "phase": judging_phase(event),
        "config": config_for(event),
        "my_record": request.user.records.filter(event=event, kind="judge", revoked_at__isnull=True).first(),
    })


@login_required
def score(request, slug, pk):
    event, role = _judge_event(request, slug)
    role, assignment = my_assignment(request.user, event, pk)
    if assignment is None:
        raise Http404()
    project = assignment.project
    config = config_for(event)
    criteria, rubric = rubric_for(event, config)
    existing = Score.objects.filter(assignment=assignment).prefetch_related("items").first()
    marks = {item.criterion_id: item.value for item in existing.items.all()} if existing else {}
    comment = existing.comment if existing else ""
    error, status = None, 200

    if request.method == "POST":
        marks = {c.pk: request.POST.get(f"c_{c.pk}") for c in criteria}
        comment = request.POST.get("comment", "")
        action = request.POST.get("action", "draft")
        try:
            saved = save_score(request.user, event, project.pk, marks, comment,
                               submit=action in ("submit", "submit_next"))
        except NotAssigned:
            raise Http404()
        except (JudgingError, WindowError) as exc:
            error = exc.message
            status = 403 if isinstance(exc, WindowError) else getattr(exc, "status", 400)
            marks = {k: int(v) for k, v in marks.items() if v not in (None, "") and str(v).isdigit()}
        else:
            if saved.submitted_at:
                messages.success(request, f"Score for “{project.title}” saved.")
                upcoming = next((r for r in _queue(request.user, event) if r["status"] != "submitted"), None)
                if upcoming and action == "submit_next":
                    return redirect("judging:score", slug=slug, pk=upcoming["project"].pk)
                return redirect("judging:queue", slug=slug)
            messages.success(request, "Draft saved. It doesn't count until you submit it.")
            return redirect("judging:score", slug=slug, pk=project.pk)

    rows = _queue(request.user, event)
    position = next((i for i, r in enumerate(rows, start=1) if r["project"].pk == project.pk), None)
    upcoming = next((r["project"] for r in rows if r["status"] != "submitted" and r["project"].pk != project.pk), None)
    scale = list(range(config.scale_min, config.scale_max + 1))
    weights = rubric.normalized_weights() if criteria else {}
    form_rows = [{"criterion": c, "value": marks.get(c.pk), "weight": weights.get(str(c.pk), 0),
                  "share": round(100 * weights.get(str(c.pk), 0))} for c in criteria]
    return render(request, "judging/judge_score.html", {
        "event": event, "role": role, "assignment": assignment, "project": project, "score": existing,
        "form_rows": form_rows, "scale": scale, "comment": comment, "error": error,
        "phase": judging_phase(event), "position": position, "count": len(rows), "upcoming": upcoming,
        "answers": project.answers.filter(question__is_public=True).select_related("question"),
        "images": project.images.all(), "members": project.team.memberships.select_related("user"),
        "total": weighted(existing, rubric) if existing and existing.submitted_at else None,
    }, status=status)


@login_required
@require_POST
def conflict(request, slug, pk):
    event, role = _judge_event(request, slug)
    try:
        declare_conflict(request.user, event, pk, request.POST.get("reason", ""))
    except NotAssigned:
        raise Http404()
    except JudgingError as exc:
        messages.error(request, exc.message)
        return redirect("judging:score", slug=slug, pk=pk)
    messages.success(request, "Noted. That project is off your queue and the organizers can see why.")
    return redirect("judging:queue", slug=slug)


def invite(request, token):
    invite = get_object_or_404(JudgeInvite.objects.select_related("event"), token=token)
    if request.method == "POST":
        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path())
        try:
            accept_invite(request.user, invite)
        except JudgingError as exc:
            messages.error(request, exc.message)
            return redirect("judging:invite", token=token)
        messages.success(request, f"You're a judge for {invite.event.name}.")
        return redirect("judging:queue", slug=invite.event.slug)
    already = request.user.is_authenticated and judge_role(request.user, invite.event) is not None
    return render(request, "judging/invite.html", {
        "invite": invite, "event": invite.event, "tracks": describe_tracks(invite), "already": already,
    })
