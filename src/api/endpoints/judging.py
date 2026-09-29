"""Judging: the organizer's rubric, judges, invites, conflicts and
assignments; the judge's queue, scorecard and conflicts; results."""

from django.shortcuts import get_object_or_404

from events.deadline import assert_assignment_allowed
from events.models import EventRole
from judging import api as judging_api
from judging import assigning, publishing, staff
from judging.access import judge_role, judged_events, my_assignment
from judging.forms import (BatchForm, ConfigForm, ConflictForm, CriterionForm, JudgeAddForm, JudgeInviteForm,
                           JudgeTracksForm, ManualAssignForm)
from judging.judge_views import _queue
from judging.models import Assignment, AssignmentBatch, Conflict, Criterion, JudgeInvite, Score
from judging.results import compute_progress
from judging.scoring import declare_conflict, rubric_for, save_score, weighted

from .. import schema as sc
from .. import serializers as s
from ..core import ApiError, endpoint, json_body, paginate, valid
from .events import current, event_for, patch_form


# What each endpoint takes and returns (api/schema.py), for the OpenAPI document.
JUDGE_STATUS = sc.enum("not_started", "in_progress", "unassigned", "done")
CONFIG = sc.obj(scale_min=sc.integer(), scale_max=sc.integer(),
                reviews_per_project=sc.integer("How many judges should review each project."),
                kappa=sc.number("Shrinkage strength for normalization (JUDGING.md, 4.3)."),
                locked=sc.boolean("Scoring has started, so the scale is fixed."))
JUDGE_BODY = sc.form_schema(JudgeAddForm, event=True)
JUDGE_TRACKS = sc.form_schema(JudgeTracksForm, event=True, partial=True)
UPDATED_JUDGE = sc.obj(**sc.COMPONENTS["JudgeRole"]["properties"],
                       reviews_removed=sc.integer("Unfinished reviews outside the new tracks, taken off the queue."))
UPDATED_JUDGE["required"] = sc.COMPONENTS["JudgeRole"]["required"] + ["reviews_removed"]
INVITE_BODY = sc.form_schema(JudgeInviteForm, event=True, optional_fields=("days",))
CONFLICT = sc.obj(id=sc.integer(), judge=sc.ref("Person"), project=sc.integer(), reason=sc.string())
ASSIGNMENT_QUERY = {"judge": sc.integer("A judge's user id."), "track": sc.integer("A track id."),
                    "status": sc.enum("pending", "draft", "submitted")}
BATCH_BODY = sc.form_schema(BatchForm, event=True, required=(), extra={
    "preview": (sc.boolean("Plan without saving. Send the returned seed back to save exactly that plan."), False)})
PLAN = sc.obj(saved=sc.boolean(), batch=sc.nullable(sc.ref("Batch")), seed=sc.integer("The seed this plan used."),
              new_reviews=sc.integer(),
              plan=sc.array(sc.obj(project=sc.integer(), title=sc.string(), judges=sc.array(sc.ref("Person")))),
              shortfalls=sc.array(sc.obj(project=sc.integer(), missing=sc.integer("Judges still needed."))))
PROGRESS = sc.obj(
    event=sc.string(), reviews_per_project=sc.integer(),
    totals=sc.obj(assignments=sc.integer(), submitted=sc.integer(), percent=sc.integer(), judges=sc.integer(),
                  not_started=sc.integer(), in_progress=sc.integer(), projects=sc.integer(),
                  complete=sc.integer("Projects with all their reviews in."),
                  short=sc.integer("Projects with fewer judges than they need.")),
    judges=sc.array(sc.obj(id=sc.integer(), external_id=sc.string(), name=sc.string(), assigned=sc.integer(),
                           submitted=sc.integer(), drafts=sc.integer(), status=JUDGE_STATUS,
                           last_activity=sc.nullable(sc.when()))),
    projects=sc.array(sc.obj(id=sc.integer(), title=sc.string(), assigned=sc.integer(), submitted=sc.integer(),
                             status=sc.string())))
RANKING = sc.obj(rank=sc.nullable(sc.integer("Competition ranking: ties share a place.")), project_id=sc.integer(),
                 title=sc.string(), reviews=sc.integer(),
                 informative_reviews=sc.integer("Reviews by judges whose marks vary, so carry signal."),
                 raw_mean=sc.nullable(sc.number()), raw_rank=sc.nullable(sc.integer()),
                 normalized_z=sc.nullable(sc.number()), normalized_score=sc.nullable(sc.number("On the marking scale.")),
                 movement=sc.nullable(sc.integer("Places gained (positive) or lost by normalizing.")))
RESULTS = sc.any_of(
    sc.obj("Not enough scores yet.", ready={"type": "boolean", "enum": [False]}, reason=sc.string()),
    sc.obj(ready={"type": "boolean", "enum": [True]}, method=sc.string(), kappa=sc.number(),
           population=sc.obj(mean=sc.number(), stdev=sc.number()), rankings=sc.array(RANKING)))
QUEUE_ITEM = sc.obj(project=sc.integer(), title=sc.string(), track=sc.nullable(sc.string()),
                    status=sc.enum("todo", "draft", "submitted"))
SCORECARD = sc.obj(
    project=sc.ref("Project"), rubric=sc.array(sc.ref("Criterion")),
    scale=sc.array(sc.integer(), "The lowest and highest mark."),
    score=sc.nullable(sc.obj(marks=sc.mapping(sc.integer(), "Your marks, by criterion key."), comment=sc.string(),
                             submitted=sc.boolean(), weighted=sc.nullable(sc.number()))))
SCORE_BODY = sc.obj(criteria=sc.mapping(sc.integer(), "Marks by criterion key, within the scale."),
                    comment=sc.optional(sc.string()),
                    submit=sc.optional(sc.boolean("Submit (it then counts); otherwise it's a draft.")))
SCORE = sc.obj(event=sc.string(), project=sc.obj(id=sc.integer(), external_id=sc.string(), title=sc.string(),
                                                 track=sc.nullable(sc.string())),
               criteria=sc.mapping(sc.integer(), "Marks by criterion key."),
               weighted_score=sc.nullable(sc.number()), comment=sc.string(), submitted=sc.boolean(),
               submitted_at=sc.nullable(sc.when()), source=sc.string())
sc.component("Score", SCORE)
PUBLISH_STATE = sc.enum("not_ready", "unpublished", "published",
                        description="not_ready until submissions close; published once an organizer publishes.")
PUBLISHED = sc.obj(state=PUBLISH_STATE, published_at=sc.nullable(sc.when()),
                   share_feedback=sc.boolean("Teams see their own marks and the judges' comments, without names."),
                   judging_ends_at=sc.nullable(sc.when("Publishing ends judging, if it hadn't ended.")))
PUBLISH_BODY = sc.obj(share_feedback=sc.optional(sc.boolean("Share each team's marks and comments with it.")),
                      announce_winners=sc.optional(sc.boolean("If winners are due later, announce them now.")))
PUBLIC_ROW = sc.obj(rank=sc.integer("Competition ranking: tied scores share a place."), project=sc.integer(),
                    title=sc.string(), team=sc.string(), track=sc.nullable(sc.string()),
                    score=sc.number("On the rubric's scale, after normalization."), reviews=sc.integer())
FEEDBACK = sc.obj(reviews=sc.integer(), scale=sc.array(sc.integer(), "The lowest and highest mark."),
                  criteria=sc.array(sc.obj(label=sc.string(), description=sc.string(),
                                           average=sc.nullable(sc.number()))),
                  comments=sc.array(sc.string(), "The judges' comments, in no particular order, without names."))
RESULTS_PAGE = sc.obj(
    "The event's results page, for whoever asks: parts that aren't public yet are null or empty.",
    state=PUBLISH_STATE, published_at=sc.nullable(sc.when()),
    ranking=sc.nullable(sc.array(PUBLIC_ROW, "The judges' ranking, once published (always, for organizers).")),
    winners=sc.array(sc.obj(prize=sc.string(), project=sc.integer(), title=sc.string(), team=sc.string(),
                            note=sc.nullable(sc.string())), "Prizes given, once the winners are announced."),
    winners_public=sc.boolean(),
    vote=sc.nullable(sc.obj(state=sc.enum("hidden", "awaiting_review", "published"),
                            top=sc.nullable(sc.array(sc.obj(rank=sc.nullable(sc.integer()), project=sc.integer(),
                                                            title=sc.string(), votes=sc.integer(),
                                                            backers=sc.integer())))),
                    ),
    yours=sc.nullable(sc.obj("Your team's project, when you're on a team.", project=sc.integer(), title=sc.string(),
                             place=sc.nullable(sc.obj(rank=sc.integer(), score=sc.number(), of=sc.integer(),
                                                      reviews=sc.integer())),
                             feedback=sc.nullable(FEEDBACK))))
JUDGE_SCORES = sc.obj(judge=sc.obj(id=sc.integer(), external_id=sc.string(), name=sc.string()), count=sc.integer(),
                      scores=sc.array(SCORE))

PROGRESS_UI = "Console, Judging, Progress"


def _organizer(request, slug):
    event, _ = event_for(request, slug, organizer=True)
    return event


def _pass(view):
    """Hand the request to an existing JSON view (it already applies every rule)."""
    def run(request, **kwargs):
        return view(request, **kwargs)
    return run


# --------------------------------------------------------- settings, rubric

@endpoint("GET", "events/<slug:slug>/judging/config", who="organizer", summary="Judging settings: scale, reviews, kappa.",
          ui="Console, Judging, Rubric, Settings",
          returns=CONFIG)
def get_config(request, slug):
    return _config_json(_organizer(request, slug))


def _config_json(event):
    c = staff.config_for(event)
    return {"scale_min": c.scale_min, "scale_max": c.scale_max, "reviews_per_project": c.reviews_per_project,
            "kappa": c.kappa, "locked": staff.scoring_started(event)}


@endpoint("PATCH", "events/<slug:slug>/judging/config", who="organizer",
          summary="Change them. The scale locks once the first score is in.", ui="Console, Judging, Rubric, Save settings",
          body='{"reviews_per_project": 3, "kappa": 5}',
          request=sc.form_schema(ConfigForm, partial=True, locked=False), returns=CONFIG)
def update_config(request, slug):
    event = _organizer(request, slug)
    config = staff.config_for(event)
    locked = staff.scoring_started(event)
    staff.save_config(request.user, event, patch_form(ConfigForm, config, json_body(request), locked=locked))
    return _config_json(event)


@endpoint("GET", "events/<slug:slug>/judging/criteria", who="organizer", summary="The rubric.", ui="Console, Judging, Rubric",
          returns=sc.results(sc.ref("Criterion")))
def criteria(request, slug):
    return {"results": [s.criterion(c) for c in _organizer(request, slug).criteria.all()]}


@endpoint("POST", "events/<slug:slug>/judging/criteria", who="organizer", summary="Add a criterion (before scoring starts).",
          ui="Console, Judging, Rubric, Add criterion", body='{"label": "Impact", "weight": 2}',
          request=sc.form_schema(CriterionForm, event=True, locked=False), returns=sc.ref("Criterion"))
def add_criterion(request, slug):
    event = _organizer(request, slug)
    locked = staff.scoring_started(event)
    data = current(CriterionForm(event=event, locked=locked))
    data.update(json_body(request))
    return s.criterion(staff.save_criterion(request.user, event, valid(CriterionForm(data, event=event, locked=locked))))


@endpoint("PATCH", "events/<slug:slug>/judging/criteria/<int:pk>", who="organizer",
          summary="Change a criterion. Weights can always move; results recalculate.",
          ui="Console, Judging, Rubric, Edit",
          request=sc.form_schema(CriterionForm, partial=True, event=True, locked=False),
          returns=sc.ref("Criterion"))
def update_criterion(request, slug, pk):
    event = _organizer(request, slug)
    obj = get_object_or_404(Criterion, pk=pk, event=event)
    form = patch_form(CriterionForm, obj, json_body(request), event=event, locked=staff.scoring_started(event))
    return s.criterion(staff.save_criterion(request.user, event, form, obj))


@endpoint("DELETE", "events/<slug:slug>/judging/criteria/<int:pk>", who="organizer",
          summary="Remove a criterion (before scoring starts).", ui="Console, Judging, Rubric, Remove",
          returns=sc.DELETED)
def delete_criterion(request, slug, pk):
    event = _organizer(request, slug)
    return {"deleted": staff.delete_criterion(request.user, event, get_object_or_404(Criterion, pk=pk, event=event))}


# ------------------------------------------------------------------ judges

def _role(event, pk):
    return get_object_or_404(EventRole, pk=pk, event=event, role=EventRole.Role.JUDGE)


@endpoint("GET", "events/<slug:slug>/judging/judges", who="organizer", summary="Judges, their tracks and progress.",
          ui="Console, Judging, Judges",
          returns=sc.results(sc.ref("JudgeRole")))
def judges(request, slug):
    event = _organizer(request, slug)
    progress = {row.user.pk: row for row in compute_progress(event)["judges"]}
    return {"results": [s.judge_role(r, progress.get(r.user_id))
                        for r in assigning.judge_roles(event).order_by("user__name")]}


@endpoint("POST", "events/<slug:slug>/judging/judges", who="organizer", summary="Make someone with an account a judge.",
          ui="Console, Judging, Judges, Add judge", body='{"email": "...", "tracks": [1, 2], "all_tracks": false}',
          request=JUDGE_BODY, returns=sc.ref("JudgeRole"))
def add_judge(request, slug):
    event = _organizer(request, slug)
    form = valid(JudgeAddForm(json_body(request), event=event))
    role = staff.add_judge(request.user, event, form.user, form.cleaned_data["tracks"], form.cleaned_data["all_tracks"])
    return s.judge_role(role)


@endpoint("PATCH", "events/<slug:slug>/judging/judges/<int:pk>", who="organizer",
          summary="Change a judge's tracks. Unfinished reviews outside them come off their queue.",
          ui="Console, Judging, Judges, Tracks", body='{"tracks": [1], "all_tracks": false}',
          request=JUDGE_TRACKS, returns=UPDATED_JUDGE)
def update_judge(request, slug, pk):
    event = _organizer(request, slug)
    role = _role(event, pk)
    data = {"tracks": [t.pk for t in role.tracks.all()], "all_tracks": role.all_tracks, **json_body(request)}
    form = valid(JudgeTracksForm(data, event=event))
    removed = staff.update_judge_tracks(request.user, role, form.cleaned_data["tracks"], form.cleaned_data["all_tracks"])
    return {**s.judge_role(role), "reviews_removed": removed}


@endpoint("DELETE", "events/<slug:slug>/judging/judges/<int:pk>", who="organizer",
          summary="Stop someone judging. Unfinished reviews go; submitted scores stay.", ui="Console, Judging, Judges, Remove",
          returns=sc.obj(reviews_removed=sc.integer(), scores_kept=sc.integer()))
def remove_judge(request, slug, pk):
    removed, kept = staff.remove_judge(request.user, _role(_organizer(request, slug), pk))
    return {"reviews_removed": removed, "scores_kept": kept}


@endpoint("GET", "events/<slug:slug>/judging/invites", who="organizer", summary="Judge invite links.",
          ui="Console, Judging, Judges, Invites",
          returns=sc.results(sc.ref("JudgeInvite")))
def invites(request, slug):
    event = _organizer(request, slug)
    return {"results": [s.invite(i, request) for i in event.judge_invites.prefetch_related("tracks")]}


@endpoint("POST", "events/<slug:slug>/judging/invites", who="organizer", summary="Make a single-use judge invite link.",
          ui="Console, Judging, Judges, Make invite link", body='{"email": "", "tracks": [1], "all_tracks": false, "days": 14}',
          request=INVITE_BODY, returns=sc.ref("JudgeInvite"))
def create_invite(request, slug):
    event = _organizer(request, slug)
    data = {"days": 14, **json_body(request)}
    c = valid(JudgeInviteForm(data, event=event)).cleaned_data
    return s.invite(staff.create_invite(request.user, event, c["email"], c["tracks"], c["all_tracks"], c["days"]), request)


@endpoint("DELETE", "events/<slug:slug>/judging/invites/<int:pk>", who="organizer", summary="Withdraw an invite link.",
          ui="Console, Judging, Judges, Withdraw",
          returns=sc.ref("JudgeInvite"))
def revoke_invite(request, slug, pk):
    event = _organizer(request, slug)
    invite = get_object_or_404(JudgeInvite, pk=pk, event=event)
    staff.revoke_invite(request.user, invite)
    return s.invite(invite)


@endpoint("POST", "judge-invites/<str:token>/accept", who="signed in", summary="Accept a judge invite for yourself.",
          ui="Judge invite page, Accept",
          returns=sc.obj(event=sc.string(), judging=sc.boolean()))
def accept_invite(request, token):
    invite = get_object_or_404(JudgeInvite.objects.select_related("event"), token=token)
    staff.accept_invite(request.user, invite)
    return {"event": invite.event.slug, "judging": True}


@endpoint("GET", "events/<slug:slug>/judging/conflicts", who="organizer", summary="Declared conflicts of interest.",
          ui="Console, Judging, Judges, Conflicts",
          returns=sc.results(CONFLICT))
def conflicts(request, slug):
    event = _organizer(request, slug)
    return {"results": [{"id": c.pk, "judge": s.person(c.judge), "project": c.project_id, "reason": c.reason}
                        for c in Conflict.objects.filter(event=event).select_related("judge")]}


@endpoint("POST", "events/<slug:slug>/judging/conflicts", who="organizer",
          summary="Record a conflict: that judge is never given that project.", ui="Console, Judging, Judges, Record conflict",
          body='{"judge": 12, "project": 34, "reason": "..."}',
          request=sc.form_schema(ConflictForm, event=True), returns=sc.obj(recorded=sc.boolean()))
def add_conflict(request, slug):
    event = _organizer(request, slug)
    c = valid(ConflictForm(json_body(request), event=event)).cleaned_data
    staff.add_conflict(request.user, event, c["judge"], c["project"], c["reason"])
    return {"recorded": True}


# ------------------------------------------------------------- assignments

@endpoint("GET", "events/<slug:slug>/judging/assignments", who="organizer",
          summary="Who reviews what; ?judge=, ?track=, ?status=pending|draft|submitted.", ui="Console, Judging, Assignments",
          query=ASSIGNMENT_QUERY, returns=sc.page(sc.ref("Assignment")))
def assignments(request, slug):
    event = _organizer(request, slug)
    rows = Assignment.objects.filter(event=event).select_related("judge", "project", "batch", "score")
    judge, track, status = (request.GET.get(k, "") for k in ("judge", "track", "status"))
    if judge.isdigit():
        rows = rows.filter(judge_id=int(judge))
    if track.isdigit():
        rows = rows.filter(project__track_id=int(track))
    if status == "pending":
        rows = rows.filter(score__isnull=True)
    elif status == "draft":
        rows = rows.filter(score__isnull=False, score__submitted_at__isnull=True)
    elif status == "submitted":
        rows = rows.filter(score__submitted_at__isnull=False)
    return paginate(request, rows.order_by("project__title", "judge__name"), s.assignment)


@endpoint("POST", "events/<slug:slug>/judging/assignments", who="organizer",
          summary="Assign one project to one judge by hand (tracks and conflicts still apply).",
          ui="Console, Judging, Assignments, Assign by hand", body='{"judge": 12, "project": 34}',
          request=sc.form_schema(ManualAssignForm, event=True), returns=sc.ref("Assignment"))
def assign(request, slug):
    event = _organizer(request, slug)
    c = valid(ManualAssignForm(json_body(request), event=event)).cleaned_data
    return s.assignment(assigning.assign_manually(request.user, event, c["judge"], c["project"]))


@endpoint("DELETE", "events/<slug:slug>/judging/assignments/<int:pk>", who="organizer",
          summary="Take an unscored assignment off a judge's queue.", ui="Console, Judging, Assignments, Remove",
          returns=sc.obj(removed=sc.boolean()))
def unassign(request, slug, pk):
    event = _organizer(request, slug)
    assigning.remove_assignment(request.user, get_object_or_404(Assignment, pk=pk, event=event))
    return {"removed": True}


@endpoint("GET", "events/<slug:slug>/judging/batches", who="organizer", summary="Assignment batches, newest first.",
          ui="Console, Judging, Assignments, Batches",
          returns=sc.results(sc.ref("Batch")))
def batches(request, slug):
    event = _organizer(request, slug)
    return {"results": [s.batch(b) for b in AssignmentBatch.objects.filter(event=event).order_by("-created_at")]}


@endpoint("POST", "events/<slug:slug>/judging/batches", who="organizer",
          summary="Run the assignment engine. \"preview\": true plans without saving; send the returned seed to commit the same plan.",
          ui="Console, Judging, Assignments, Preview / Save this batch",
          body='{"label": "Batch 2", "scope": "gaps|track|chosen", "track": 1, "projects": [], "judges_scope": "all|chosen", "judges": [], "target": 3, "seed": 2026, "preview": true}',
          request=BATCH_BODY, returns=PLAN)
def run_batch(request, slug):
    event = _organizer(request, slug)
    body = json_body(request)
    data = {"label": "API batch", "scope": "gaps", "judges_scope": "all",
            "target": staff.config_for(event).reviews_per_project, **body}
    form = valid(BatchForm(data, event=event))
    params, scope = form.scope_params()
    c = form.cleaned_data
    if body.get("preview"):
        assert_assignment_allowed(event)
        projects, roles = assigning.scoped_inputs(event, **params)
        proposal = assigning.plan_batch(event, projects, roles, c["target"], seed=c["seed"], scope=scope)
        saved = None
    else:
        saved, proposal = assigning.run_batch(request.user, event, label=c["label"], target=c["target"],
                                              seed=c["seed"], scope=scope, **params)
    return {"saved": saved is not None, "batch": s.batch(saved) if saved else None, "seed": proposal.seed,
            "new_reviews": proposal.total,
            "plan": [{"project": p.pk, "title": p.title, "judges": [s.person(u) for u in users]} for p, users in proposal.rows],
            "shortfalls": [{"project": p.pk, "missing": n} for p, n in proposal.shortfalls]}


judging_progress = endpoint("GET", "events/<slug:slug>/judging/progress", who="organizer",
                            summary="Live progress: who hasn't started, what's short of judges.", ui=PROGRESS_UI,
                            returns=PROGRESS)(_pass(judging_api.progress))
judging_results = endpoint("GET", "events/<slug:slug>/judging/results", who="organizer",
                           summary="Normalized rankings, with the raw ranking beside them.",
                           ui="Console, Judging, Results", returns=RESULTS)(_pass(judging_api.results))
export_csv = endpoint("GET", "events/<slug:slug>/export/<str:stage>.csv", who="organizer",
                      summary="CSV export of one stage (teams, projects, judges, assignments, raw-scores, "
                              "normalized-scores, final-rankings, score-history, community-votes, ballots, comments, audit-log).",
                      ui="Console, CSV exports", produces="text/csv")(_pass(judging_api.export))


def _published(event):
    config = staff.config_for(event)
    event.refresh_from_db()
    return {"state": publishing.state(event, config), "published_at": s.when(config.results_published_at),
            "share_feedback": config.share_feedback, "judging_ends_at": s.when(event.judging_ends_at)}


@endpoint("POST", "events/<slug:slug>/judging/publish", who="organizer",
          summary="Publish the judges' results on the event's results page. Ends judging now if it's still open.",
          ui="Console, Judging, Results, Publish the results", body='{"share_feedback": true, "announce_winners": true}',
          request=PUBLISH_BODY, returns=PUBLISHED)
def publish_results(request, slug):
    event = _organizer(request, slug)
    body = json_body(request)
    publishing.publish(request.user, event, share_feedback=body.get("share_feedback") is True,
                       announce_winners=body.get("announce_winners") is True)
    return _published(event)


@endpoint("POST", "events/<slug:slug>/judging/unpublish", who="organizer",
          summary="Take the published results down. Judging stays closed.",
          ui="Console, Judging, Results, Take them down", returns=PUBLISHED)
def unpublish_results(request, slug):
    event = _organizer(request, slug)
    publishing.unpublish(request.user, event)
    return _published(event)


@endpoint("GET", "events/<slug:slug>/results", who="anyone", auth=False,
          summary="The results page: winners, the judges' ranking and the community vote, each once public; "
                  "your team's place and feedback. ?track= filters the ranking.",
          ui="Results page", query={"track": sc.integer("Only this track's projects.")}, returns=RESULTS_PAGE)
def results_page(request, slug):
    event, viewer = event_for(request, slug)
    track = request.GET.get("track", "")
    data = publishing.page(viewer, event, int(track) if track.isdigit() else None)
    yours = data["yours"]
    return {
        "state": data["state"], "published_at": s.when(data["config"].results_published_at),
        "ranking": [{"rank": r.rank, "project": r.project.pk, "title": r.project.title, "team": r.project.team.name,
                     "track": r.project.track.name if r.project.track else None, "score": round(r.display, 3),
                     "reviews": r.reviews} for r in data["rows"]] if data["show_ranking"] else None,
        "winners": [{"prize": a.prize.name, "project": a.project_id, "title": a.project.title,
                     "team": a.project.team.name, "note": a.note or None} for a in data["awards"]],
        "winners_public": data["awards_public"],
        "vote": None if not data["vote_state"] else {
            "state": data["vote_state"],
            "top": [{"rank": line["rank"], "project": line["project"].pk, "title": line["project"].title,
                     "votes": line["votes"], "backers": line["supporters"]} for line in data["vote_lines"]]
            if data["show_vote"] else None},
        "yours": None if not yours else {
            "project": yours["project"].pk, "title": yours["project"].title,
            "place": None if not yours["place"] else {**yours["place"], "score": round(yours["place"]["score"], 3)},
            "feedback": None if not yours["feedback"] else {**yours["feedback"],
                                                            "scale": list(yours["feedback"]["scale"])}},
    }


# ------------------------------------------------------------ the judge's side

def _judge_event(request, slug):
    event, _ = event_for(request, slug)
    if judge_role(request.user, event) is None:
        raise ApiError(404, "not_found", "You don't judge this event.")
    return event


@endpoint("GET", "judge/events", who="judge", summary="The events you judge.", ui="Judging home",
          returns=sc.results(sc.obj(slug=sc.string(), name=sc.string())))
def judge_home(request):
    return {"results": [{"slug": e.slug, "name": e.name} for e in judged_events(request.user)]}


@endpoint("GET", "judge/events/<slug:slug>/queue", who="judge",
          summary="Your queue: only the projects assigned to you, in your tracks.", ui="Your judging queue",
          returns=sc.results(QUEUE_ITEM))
def queue(request, slug):
    event = _judge_event(request, slug)
    return {"results": [{"project": r["project"].pk, "title": r["project"].title,
                         "track": r["project"].track.name if r["project"].track else None, "status": r["status"]}
                        for r in _queue(request.user, event)]}


@endpoint("GET", "judge/events/<slug:slug>/projects/<int:pk>", who="judge",
          summary="One project on your queue with your scorecard. Anything else is a 404.", ui="Scorecard",
          returns=SCORECARD)
def scorecard(request, slug, pk):
    return _scorecard_json(request, _judge_event(request, slug), pk)


def _scorecard_json(request, event, pk):
    _, a = my_assignment(request.user, event, pk)
    if a is None:
        raise ApiError(404, "not_found", "No such project in your judging queue.")
    criteria_list, rubric = rubric_for(event)
    score = Score.objects.filter(assignment=a).prefetch_related("items__criterion").first()
    from projects.api import serialize
    project = serialize(a.project, request)
    project["answers"] = [x for x, ans in zip(project["answers"], a.project.answers.select_related("question"))
                          if ans.question.is_public]
    return {"project": project, "rubric": [s.criterion(c) for c in criteria_list],
            "scale": [staff.config_for(event).scale_min, staff.config_for(event).scale_max],
            "score": None if score is None else {
                "marks": {i.criterion.key: i.value for i in score.items.all()}, "comment": score.comment,
                "submitted": score.submitted_at is not None,
                "weighted": weighted(score, rubric) if score.submitted_at else None}}


@endpoint("PUT", "judge/events/<slug:slug>/projects/<int:pk>/score", who="judge",
          summary="Save a draft or submit your score. Marks keyed by criterion key. Refused outside the judging window.",
          ui="Scorecard, Save draft / Submit score",
          body='{"criteria": {"functionality": 4, "quality": 3}, "comment": "", "submit": true}',
          request=SCORE_BODY, returns=SCORECARD)
def put_score(request, slug, pk):
    event = _judge_event(request, slug)
    body = json_body(request)
    by_key = {c.key: c.pk for c in event.criteria.all()}
    marks = {}
    for key, value in (body.get("criteria") or {}).items():
        if key not in by_key:
            raise ApiError(400, "invalid", f"Unknown criterion {key!r}.")
        marks[by_key[key]] = value
    save_score(request.user, event, pk, marks, str(body.get("comment") or ""), submit=bool(body.get("submit")))
    return _scorecard_json(request, event, pk)


@endpoint("POST", "judge/events/<slug:slug>/projects/<int:pk>/conflict", who="judge",
          summary="Step away from a project you have a conflict with.", ui="Scorecard, I have a conflict of interest",
          body='{"reason": "I mentored this team"}',
          request=sc.obj(reason=sc.string("Briefly, why.")), returns=sc.obj(recorded=sc.boolean()))
def judge_conflict(request, slug, pk):
    event = _judge_event(request, slug)
    declare_conflict(request.user, event, pk, json_body(request).get("reason", ""))
    return {"recorded": True}


judge_scores = endpoint("GET", "judge/scores", who="judge",
                        summary="Your own scores. ?judge=<someone else> is refused unless you organize the event.",
                        ui="Judging, your scores", returns=JUDGE_SCORES,
                        query={"judge": sc.string("Someone else's id or external id: only for organizers."),
                               "event": sc.string("Only this event's scores.")})(_pass(judging_api.judge_scores))
