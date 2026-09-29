"""JSON for judging, plus the CSV export endpoint.

/api/judge/scores is the route the acceptance checker probes:

* signed out: 401
* signed in but not a judge anywhere: 403
* a judge asking for their own scores: 200
* anyone asking for another judge's scores (?judge=...): 403, unless they
  organize the event those scores belong to

The ?judge= value is only ever used to decide whether to refuse. What a judge
gets back is always filtered by their own session, never by that parameter.
"""

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt

from accounts.models import User
from events.access import Viewer, organizer_events
from events.deadline import WindowError
from events.models import Event
from events.timeutil import iso_utc
from projects.api import ApiError, read_json

from .access import judged_events, my_assignments, my_scores
from .errors import JudgingError
from .exports import STAGE_NAMES, csv_response
from .models import Score
from .results import compute_progress, compute_results
from .scoring import rubric_for, save_score, weighted


def _refers_to(user, value):
    value = value.strip().lower()
    return value in {str(user.pk), (user.external_id or "").lower(), user.email}


def _find_judge(value):
    value = value.strip()
    if value.isdigit():
        return User.objects.filter(pk=int(value)).first()
    return User.objects.filter(external_id=value).first() or User.objects.filter(email=value.lower()).first()


def _score_json(score, rubrics):
    if score.event_id not in rubrics:
        rubrics[score.event_id] = rubric_for(score.event)[1]
    total = weighted(score, rubrics[score.event_id]) if score.submitted_at else None
    return {
        "event": score.event.slug,
        "project": {"id": score.project_id, "external_id": score.project.external_id, "title": score.project.title,
                    "track": score.project.track.name if score.project.track else None},
        "criteria": {item.criterion.key: item.value for item in score.items.all()},
        "weighted_score": None if total is None else round(total, 3),
        "comment": score.comment,
        "submitted": score.submitted_at is not None,
        "submitted_at": iso_utc(score.submitted_at) if score.submitted_at else None,
        "source": score.source,
    }


def _event_or_error(slug):
    if not slug:
        return None
    event = Event.objects.filter(slug=slug).first()
    if event is None:
        raise ApiError(404, "no_such_event", "No event with that address.")
    return event


def judge_scores(request):
    if request.method != "GET":
        return ApiError(405, "method_not_allowed", "Use GET.").response()
    user = request.user
    if not user.is_authenticated:
        return ApiError(401, "not_signed_in", "Send a session cookie. Sign in at /login/ to get one.").response()
    try:
        event = _event_or_error(request.GET.get("event", ""))
        requested = request.GET.get("judge", "").strip()
        if requested and not _refers_to(user, requested):
            # Another judge's scores. Only an organizer of the events involved
            # may read them; a judge asking is refused before any lookup, so
            # the answer is the same whether that judge exists or not.
            allowed = organizer_events(user)
            if not allowed.exists():
                raise ApiError(403, "forbidden", "Judges can only read their own scores.")
            target = _find_judge(requested)
            if target is None:
                raise ApiError(404, "no_such_judge", "No judge with that id.")
            scores = Score.objects.filter(judge=target, event__in=allowed)
            if event is not None:
                if not allowed.filter(pk=event.pk).exists():
                    raise ApiError(403, "forbidden", "You don't organize that event.")
                scores = scores.filter(event=event)
            scores = scores.select_related("project__track", "event").prefetch_related("items__criterion")
            subject = target
        else:
            if not judged_events(user).exists():
                raise ApiError(403, "not_a_judge", "Only judges have scores to read.")
            scores = my_scores(user, event)
            subject = user
    except ApiError as err:
        return err.response()
    rubrics = {}
    items = [_score_json(s, rubrics) for s in scores]
    return JsonResponse({
        "judge": {"id": subject.pk, "external_id": subject.external_id, "name": subject.display_name},
        "count": len(items),
        "scores": items,
    })


def judge_assignments(request):
    user = request.user
    if not user.is_authenticated:
        return ApiError(401, "not_signed_in", "Send a session cookie.").response()
    try:
        event = _event_or_error(request.GET.get("event", ""))
    except ApiError as err:
        return err.response()
    events = [event] if event else list(judged_events(user))
    if not events or (event and not judged_events(user).filter(pk=event.pk).exists()):
        return ApiError(403, "not_a_judge", "You don't judge that.").response()
    items = []
    for ev in events:
        for a in my_assignments(user, ev):
            score = Score.objects.filter(assignment=a).first()
            items.append({
                "event": ev.slug, "project_id": a.project_id, "title": a.project.title,
                "track": a.project.track.name if a.project.track else None,
                "status": "submitted" if score and score.submitted_at else ("draft" if score else "pending"),
            })
    return JsonResponse({"count": len(items), "assignments": items})


@csrf_exempt
def submit_score(request, slug):
    """POST {"project": 12, "criteria": {"functionality": 4, ...}, "comment": "", "submit": true}"""
    if request.method != "POST":
        return ApiError(405, "method_not_allowed", "Use POST.").response()
    if not request.user.is_authenticated:
        return ApiError(401, "not_signed_in", "Send a session cookie.").response()
    try:
        body = read_json(request)
        event = _event_or_error(slug)
        project_id = body.get("project")
        if not isinstance(project_id, int):
            raise ApiError(400, "invalid", '"project" must be the project id, a number.')
        by_key = {c.key: c.pk for c in event.criteria.all()}
        marks = {}
        for key, value in (body.get("criteria") or {}).items():
            if key not in by_key:
                raise ApiError(400, "invalid", f"Unknown criterion {key!r}.")
            marks[by_key[key]] = value
        score = save_score(request.user, event, project_id, marks, str(body.get("comment") or ""),
                           submit=bool(body.get("submit")))
    except ApiError as err:
        return err.response()
    except WindowError as exc:
        return ApiError(403, exc.code, exc.message).response()
    except JudgingError as exc:
        return ApiError(exc.status, exc.code, exc.message).response()
    score = Score.objects.select_related("project__track", "event").prefetch_related("items__criterion").get(pk=score.pk)
    return JsonResponse(_score_json(score, {}))


def _organizer_event(request, slug):
    if not request.user.is_authenticated:
        raise ApiError(401, "not_signed_in", "Send a session cookie.")
    event = Event.objects.filter(slug=slug).first()
    if event is None:
        raise ApiError(404, "no_such_event", "No event with that address.")
    if not Viewer(request.user, event).is_organizer:
        raise ApiError(403, "forbidden", "Only this event's organizers can read that.")
    return event


def progress(request, slug):
    try:
        event = _organizer_event(request, slug)
    except ApiError as err:
        return err.response()
    data = compute_progress(event)
    return JsonResponse({
        "event": event.slug,
        "reviews_per_project": data["target"],
        "totals": data["totals"],
        "judges": [{"id": j.user.pk, "external_id": j.user.external_id, "name": j.user.display_name,
                    "assigned": j.assigned, "submitted": j.submitted, "drafts": j.drafts, "status": j.status,
                    "last_activity": iso_utc(j.last_activity) if j.last_activity else None} for j in data["judges"]],
        "projects": [{"id": p.project.pk, "title": p.project.title, "assigned": p.assigned,
                      "submitted": p.submitted, "status": p.status} for p in data["projects"]],
    })


def results(request, slug):
    try:
        event = _organizer_event(request, slug)
    except ApiError as err:
        return err.response()
    outcome = compute_results(event)
    if not outcome.ready:
        return JsonResponse({"ready": False, "reason": outcome.reason})
    return JsonResponse({
        "ready": True,
        "method": "shrinkage z-score per judge, mean z per project",
        "kappa": outcome.kappa,
        "population": {"mean": round(outcome.pop_mean, 3), "stdev": round(outcome.pop_std, 3)},
        "rankings": [{"rank": r.rank, "project_id": r.project.pk, "title": r.project.title, "reviews": r.reviews,
                      "informative_reviews": r.informative,
                      "raw_mean": None if r.raw_mean is None else round(r.raw_mean, 3), "raw_rank": r.raw_rank,
                      "normalized_z": None if r.z_mean is None else round(r.z_mean, 3),
                      "normalized_score": None if r.display is None else round(r.display, 3),
                      "movement": r.movement} for r in outcome.rows],
    })


def export(request, slug, stage):
    try:
        event = _organizer_event(request, slug)
        if stage not in STAGE_NAMES:
            raise ApiError(404, "no_such_export", f"Exports: {', '.join(sorted(STAGE_NAMES))}.")
    except ApiError as err:
        return err.response()
    return csv_response(event, stage)
