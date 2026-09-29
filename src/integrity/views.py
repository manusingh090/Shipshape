"""The organizer's Integrity page: rate limits, what the detectors found, and
the decisions already made. Organizers only."""

from datetime import timedelta

from django.contrib import messages
from django.http import HttpResponseBadRequest
from django.shortcuts import redirect, render
from django.utils import timezone

from accounts.decorators import login_required
from events.access import get_event_for, require_organizer
from voting.models import Ballot

from . import detect, limits, services


def _ids(values):
    out = []
    for value in values:
        try:
            out.append(int(value))
        except (TypeError, ValueError):
            continue
    return out


@login_required
def review(request, slug):
    event, viewer = get_event_for(request, slug)
    require_organizer(viewer)

    if request.method == "POST":
        op, reason = request.POST.get("op"), request.POST.get("reason", "")
        try:
            if op == "exclude":
                n = services.exclude_ballots(event, request.user, _ids(request.POST.getlist("ballot")), reason)
                messages.success(request, f"{n} ballot{'s' if n != 1 else ''} left out of the count.")
            elif op == "restore":
                ballot = services.restore_ballot(event, request.user, request.POST.get("ballot"))
                messages.success(request, f"Ballot #{ballot.pk} is counted again.")
            elif op == "hide_duplicate":
                project = services.hide_duplicate(event, request.user, request.POST.get("project"),
                                                  request.POST.get("original"), reason)
                messages.success(request, f"“{project.title}” is hidden as a duplicate.")
            elif op == "remove_comments":
                n = services.remove_repeated(event, request.user, _ids(request.POST.getlist("comment")), reason)
                messages.success(request, f"Removed {n} comment{'s' if n != 1 else ''}.")
            else:
                return HttpResponseBadRequest("Unknown operation.")
        except services.ReviewError as err:
            messages.error(request, err.message)
        return redirect(request.path)

    day_ago = timezone.now() - timedelta(days=1)
    flags = detect.ballot_flags(event)
    counted_flags = [f for f in flags if f.ballot.excluded_at is None]
    return render(request, "integrity/review.html", {
        "event": event, "viewer": viewer, "section": "integrity",
        "limits": list(limits.LIMITS.values()),
        "refusals": limits.recent_refusals(event, since=day_ago).select_related("actor")[:20],
        "refusal_count": limits.recent_refusals(event, since=day_ago).count(),
        "flags": counted_flags,
        "strong": [f for f in counted_flags if f.strength >= 2],
        "excluded": Ballot.objects.filter(event=event, excluded_at__isnull=False)
                    .select_related("excluded_by", "user").order_by("-excluded_at"),
        "duplicates": detect.duplicate_projects(event),
        "repeated": detect.repeated_comments(event),
        "disagreements": detect.judge_disagreements(event),
    })
