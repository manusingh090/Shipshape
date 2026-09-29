"""Organizer pages for judging. Every view starts with require_organizer."""

from decimal import Decimal

from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Count
from django.http import HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from accounts.decorators import login_required
from events.access import get_event_for, require_organizer
from events.deadline import WindowError, assert_assignment_allowed, judging_phase
from events.models import EventRole

from . import assigning, publishing, staff
from .errors import JudgingError
from .exports import STAGES
from .forms import (BatchForm, ConfigForm, ConflictForm, CriterionForm, JudgeAddForm, JudgeInviteForm,
                    JudgeTracksForm, ManualAssignForm)
from .models import Assignment, AssignmentBatch, Conflict, Criterion, JudgeInvite, Score
from .results import compute_progress, compute_results


def _console(request, slug):
    event, viewer = get_event_for(request, slug)
    require_organizer(viewer)
    return event, viewer


def _context(event, viewer, sub, **extra):
    return {"event": event, "viewer": viewer, "section": "judging", "sub": sub,
            "judging_phase": judging_phase(event), **extra}


@login_required
def progress(request, slug):
    event, viewer = _console(request, slug)
    context = _context(event, viewer, "progress", progress=compute_progress(event),
                       config=staff.config_for(event))
    if request.GET.get("fragment"):
        return render(request, "judging/_progress_live.html", context)
    return render(request, "judging/progress.html", context)


@login_required
def rubric(request, slug):
    event, viewer = _console(request, slug)
    config = staff.config_for(event)
    locked = staff.scoring_started(event)
    config_form = ConfigForm(instance=config, locked=locked)
    editing = None
    criterion_form = CriterionForm(event=event, locked=locked)

    if request.method == "POST":
        op = request.POST.get("op")
        if op == "config":
            config_form = ConfigForm(request.POST, instance=config, locked=locked)
            if config_form.is_valid():
                staff.save_config(request.user, event, config_form)
                messages.success(request, "Judging settings saved.")
                return redirect("judging:rubric", slug=slug)
        elif op == "save":
            pk = request.POST.get("id")
            editing = get_object_or_404(Criterion, pk=pk, event=event) if pk else None
            if editing is None and locked:
                messages.error(request, "Scoring has started, so the rubric can't gain criteria. Weights can still change.")
                return redirect("judging:rubric", slug=slug)
            criterion_form = CriterionForm(request.POST, instance=editing, event=event, locked=locked)
            if criterion_form.is_valid():
                criterion = staff.save_criterion(request.user, event, criterion_form, editing)
                messages.success(request, f"Saved “{criterion.label}”.")
                return redirect("judging:rubric", slug=slug)
        elif op == "delete":
            criterion = get_object_or_404(Criterion, pk=request.POST.get("id"), event=event)
            try:
                label = staff.delete_criterion(request.user, event, criterion)
            except JudgingError as err:
                messages.error(request, err.message)
            else:
                messages.success(request, f"Removed “{label}”.")
            return redirect("judging:rubric", slug=slug)
        else:
            return HttpResponseBadRequest("Unknown operation.")
    elif request.GET.get("edit"):
        editing = get_object_or_404(Criterion, pk=request.GET["edit"], event=event)
        criterion_form = CriterionForm(instance=editing, event=event, locked=locked)

    criteria = list(event.criteria.all())
    total = sum((c.weight for c in criteria), Decimal(0))
    shares = [(c, round(100 * c.weight / total) if total else 0) for c in criteria]
    return render(request, "judging/rubric.html", _context(
        event, viewer, "rubric", config=config, config_form=config_form, criterion_form=criterion_form,
        editing=editing, shares=shares, locked=locked,
    ))


def _invite_url(request, invite):
    return request.build_absolute_uri(reverse("judging:invite", args=[invite.token]))


@login_required
def judges(request, slug):
    event, viewer = _console(request, slug)
    add_form = JudgeAddForm(event=event)
    invite_form = JudgeInviteForm(event=event)
    conflict_form = ConflictForm(event=event)
    editing = None
    tracks_form = None

    if request.method == "POST":
        op = request.POST.get("op")
        try:
            if op == "add":
                add_form = JudgeAddForm(request.POST, event=event)
                if add_form.is_valid():
                    staff.add_judge(request.user, event, add_form.user, add_form.cleaned_data["tracks"],
                                    add_form.cleaned_data["all_tracks"])
                    messages.success(request, f"{add_form.user.display_name} is judging {event.name}.")
                    return redirect("judging:judges", slug=slug)
            elif op == "invite":
                invite_form = JudgeInviteForm(request.POST, event=event)
                if invite_form.is_valid():
                    c = invite_form.cleaned_data
                    invite = staff.create_invite(request.user, event, c["email"], c["tracks"], c["all_tracks"],
                                                 c["days"])
                    messages.success(request, "Invite link made. Copy it from the list below and send it.")
                    return redirect(f"{reverse('judging:judges', args=[slug])}?new={invite.pk}#invites")
            elif op == "revoke":
                invite = get_object_or_404(JudgeInvite, pk=request.POST.get("id"), event=event)
                staff.revoke_invite(request.user, invite)
                messages.success(request, "Invite withdrawn. The link no longer works.")
                return redirect("judging:judges", slug=slug)
            elif op == "tracks":
                editing = get_object_or_404(EventRole, pk=request.POST.get("id"), event=event,
                                            role=EventRole.Role.JUDGE)
                tracks_form = JudgeTracksForm(request.POST, event=event)
                if tracks_form.is_valid():
                    removed = staff.update_judge_tracks(request.user, editing, tracks_form.cleaned_data["tracks"],
                                                        tracks_form.cleaned_data["all_tracks"])
                    note = f" {removed} unfinished review{'s' if removed != 1 else ''} outside those tracks came off their queue." \
                        if removed else ""
                    messages.success(request, f"Updated {editing.user.display_name}'s tracks.{note}")
                    return redirect("judging:judges", slug=slug)
            elif op == "remove":
                role = get_object_or_404(EventRole, pk=request.POST.get("id"), event=event, role=EventRole.Role.JUDGE)
                removed, kept = staff.remove_judge(request.user, role)
                messages.success(request, f"{role.user.display_name} no longer judges here. {removed} unfinished "
                                          f"review{'s' if removed != 1 else ''} dropped; {kept} submitted "
                                          f"score{'s' if kept != 1 else ''} kept.")
                return redirect("judging:judges", slug=slug)
            elif op == "conflict":
                conflict_form = ConflictForm(request.POST, event=event)
                if conflict_form.is_valid():
                    c = conflict_form.cleaned_data
                    staff.add_conflict(request.user, event, c["judge"], c["project"], c["reason"])
                    messages.success(request, f"Recorded. {c['judge'].display_name} will never be given "
                                              f"“{c['project'].title}”.")
                    return redirect("judging:judges", slug=slug)
            else:
                return HttpResponseBadRequest("Unknown operation.")
        except JudgingError as exc:
            messages.error(request, exc.message)
            return redirect("judging:judges", slug=slug)
    elif request.GET.get("edit"):
        editing = get_object_or_404(EventRole, pk=request.GET["edit"], event=event, role=EventRole.Role.JUDGE)
        tracks_form = JudgeTracksForm(event=event, initial={"tracks": list(editing.tracks.all()),
                                                             "all_tracks": editing.all_tracks})

    progress_rows = {row.user.pk: row for row in compute_progress(event)["judges"]}
    roles = assigning.judge_roles(event).order_by("user__name")
    rows = [(role, progress_rows.get(role.user_id)) for role in roles]
    invites = [(invite, _invite_url(request, invite)) for invite in event.judge_invites.prefetch_related("tracks")]
    return render(request, "judging/judges.html", _context(
        event, viewer, "judges", rows=rows, invites=invites, add_form=add_form, invite_form=invite_form,
        conflict_form=conflict_form, editing=editing, tracks_form=tracks_form,
        conflicts=Conflict.objects.filter(event=event).select_related("judge", "project"),
        highlight=request.GET.get("new", ""),
    ))


def _hidden_batch_fields(form, seed):
    """The inputs that let "save this batch" reproduce the preview exactly."""
    c = form.cleaned_data
    pairs = [("label", c["label"]), ("scope", c["scope"]), ("judges_scope", c["judges_scope"]),
             ("target", c["target"]), ("seed", seed)]
    if c.get("track"):
        pairs.append(("track", c["track"].pk))
    pairs += [("projects", p.pk) for p in c.get("projects") or []]
    pairs += [("judges", u.pk) for u in c.get("judges") or []]
    return pairs


@login_required
def assignments(request, slug):
    event, viewer = _console(request, slug)
    config = staff.config_for(event)
    batch_count = AssignmentBatch.objects.filter(event=event).count()
    batch_form = BatchForm(event=event, initial={"label": f"Batch {batch_count + 1}",
                                                 "target": config.reviews_per_project})
    manual_form = ManualAssignForm(event=event)
    proposal, hidden = None, []

    if request.method == "POST":
        op = request.POST.get("op")
        try:
            if op in ("preview", "commit"):
                batch_form = BatchForm(request.POST, event=event)
                if batch_form.is_valid():
                    params, scope = batch_form.scope_params()
                    c = batch_form.cleaned_data
                    if op == "preview":
                        assert_assignment_allowed(event)
                        projects, roles = assigning.scoped_inputs(event, **params)
                        proposal = assigning.plan_batch(event, projects, roles, c["target"], seed=c["seed"],
                                                        scope=scope)
                        hidden = _hidden_batch_fields(batch_form, proposal.seed)
                    else:
                        batch, proposal = assigning.run_batch(request.user, event, label=c["label"],
                                                              target=c["target"], seed=c["seed"], scope=scope,
                                                              **params)
                        gaps = f" {batch.shortfall_count} review{'s' if batch.shortfall_count != 1 else ''} " \
                               "couldn't be placed: not enough eligible judges." if batch.shortfall_count else ""
                        messages.success(request, f"“{batch.label}” saved: {proposal.total} new "
                                                  f"review{'s' if proposal.total != 1 else ''}.{gaps}")
                        return redirect("judging:assignments", slug=slug)
            elif op == "manual":
                manual_form = ManualAssignForm(request.POST, event=event)
                if manual_form.is_valid():
                    a = assigning.assign_manually(request.user, event, manual_form.cleaned_data["judge"],
                                                  manual_form.cleaned_data["project"])
                    messages.success(request, f"Assigned “{a.project.title}” to {a.judge.display_name}.")
                    return redirect("judging:assignments", slug=slug)
            elif op == "remove":
                assignment = get_object_or_404(Assignment, pk=request.POST.get("id"), event=event)
                assigning.remove_assignment(request.user, assignment)
                messages.success(request, "Taken off their queue.")
                return redirect(request.get_full_path())
            else:
                return HttpResponseBadRequest("Unknown operation.")
        except (JudgingError, WindowError) as exc:
            messages.error(request, exc.message)
            if op in ("manual", "remove"):
                return redirect("judging:assignments", slug=slug)

    batches = AssignmentBatch.objects.filter(event=event).annotate(n=Count("assignments")).order_by("-created_at")
    rows = Assignment.objects.filter(event=event).select_related("judge", "project__track", "batch", "score")
    judge_filter, track_filter, status_filter = (request.GET.get(k, "") for k in ("judge", "track", "status"))
    if judge_filter.isdigit():
        rows = rows.filter(judge_id=int(judge_filter))
    if track_filter.isdigit():
        rows = rows.filter(project__track_id=int(track_filter))
    if status_filter == "pending":
        rows = rows.filter(score__isnull=True)
    elif status_filter == "draft":
        rows = rows.filter(score__isnull=False, score__submitted_at__isnull=True)
    elif status_filter == "submitted":
        rows = rows.filter(score__submitted_at__isnull=False)
    page = Paginator(rows.order_by("project__title", "judge__name"), 60).get_page(request.GET.get("page"))

    return render(request, "judging/assignments.html", _context(
        event, viewer, "assignments", config=config, batch_form=batch_form, manual_form=manual_form,
        proposal=proposal, hidden=hidden, batches=batches, page=page,
        judges=manual_form.fields["judge"].queryset, tracks=event.tracks.all(),
        filters={"judge": judge_filter, "track": track_filter, "status": status_filter},
        can_assign=judging_phase(event) != "not_open",
        total=Assignment.objects.filter(event=event).count(),
    ))


@login_required
def results(request, slug):
    event, viewer = _console(request, slug)
    if request.method == "POST":
        op = request.POST.get("op")
        try:
            if op == "publish":
                publishing.publish(request.user, event, share_feedback=request.POST.get("share_feedback") == "on",
                                   announce_winners=request.POST.get("announce_winners") == "on")
                messages.success(request, "Results published. Judging is closed, and everyone can see the results page.")
            elif op == "unpublish":
                publishing.unpublish(request.user, event)
                messages.success(request, "Results taken down. Only organizers can see the ranking again.")
            else:
                return HttpResponseBadRequest("Unknown operation.")
        except JudgingError as err:
            messages.error(request, err.message)
        return redirect("judging:results", slug=slug)
    event.refresh_from_db()
    config = staff.config_for(event)
    track_param = request.GET.get("track", "")
    track = event.tracks.filter(pk=int(track_param)).first() if track_param.isdigit() else None
    outcome = compute_results(event, track_id=track.pk if track else None)
    return render(request, "judging/results.html", _context(
        event, viewer, "results", results=outcome, track=track, tracks=event.tracks.all(),
        config=config, publish_state=publishing.state(event, config),
        winners_later=bool(event.results_at and event.results_at > timezone.now()),
        pending=Score.objects.filter(event=event, submitted_at__isnull=True).count(),
    ))


def public_results(request, slug):
    """The event's results page: winners, the judges' ranking and the
    community vote, each once it's been made public. A team also sees its
    own place, and the judges' feedback if the organizers shared it."""
    event, viewer = get_event_for(request, slug)
    track_param = request.GET.get("track", "")
    track = event.tracks.filter(pk=int(track_param)).first() if track_param.isdigit() else None
    return render(request, "judging/public_results.html", {
        "event": event, "viewer": viewer, "track": track, "tracks": event.tracks.all(),
        **publishing.page(viewer, event, track.pk if track else None),
    })


@login_required
def exports(request, slug):
    event, viewer = _console(request, slug)
    stages = [(key, label, note, reverse("judging:export", args=[event.slug, key])) for key, label, note in STAGES]
    return render(request, "judging/exports.html", {"event": event, "viewer": viewer, "section": "exports", "stages": stages})
