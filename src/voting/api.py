"""JSON for the community vote: /api/events/<slug>/ballot (GET and POST).

It identifies the voter exactly as the ballot page does (the session, or the
account), so a script gets no more than a browser would. For an open-link
vote, pass the link's token as ?link=<token>.

Order of checks: the gate (signed in, email confirmed, or the right link),
then valid JSON, then the voting window, then eligibility, then the ballot
itself. POST body: {"votes": {"<project id>": <votes>, ...}}; a project left
out gets no votes.
"""

import secrets

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt

from events.access import Viewer
from events.deadline import WindowError, voting_opens_at, voting_phase
from events.models import Event
from events.timeutil import iso_utc
from projects.api import ApiError, read_json

from . import services
from .models import Ballot, VotingConfig
from .results import compute_tally


def _state(event, config, voter, ballot):
    credits, ceiling = services.limits(config)
    votes = services.ballot_votes(ballot)
    refusal, own = services.eligibility(event, voter)
    return {
        "event": event.slug,
        "access": config.access,
        "method": config.method,
        "credits": credits,
        "max_votes_per_project": ceiling,
        "phase": voting_phase(event, config),
        "opens_at": iso_utc(voting_opens_at(event, config)),
        "closes_at": iso_utc(config.closes_at) if config.closes_at else None,
        "can_vote": refusal is None,
        "refusal": refusal,
        "ballot": {
            "votes": {str(pid): n for pid, n in votes.items()},
            "credits_spent": sum(n * n for n in votes.values()) if config.is_quadratic else len(votes),
            "updated_at": iso_utc(ballot.updated_at) if ballot else None,
        },
        "projects": [
            {"id": p.pk, "title": p.title, "team": p.team.name, "track": p.track.name if p.track else None,
             "yours": p.pk in own}
            for p in services.ballot_for(event, config, voter)
        ],
        "order": config.order,
    }


@csrf_exempt
def ballot(request, slug):
    if request.method not in ("GET", "POST"):
        return ApiError(405, "method_not_allowed", "Use GET or POST.").response()
    try:
        event = Event.objects.filter(slug=slug).first()
        if event is None or not Viewer(request.user, event).can_view_event:
            raise ApiError(404, "no_such_event", "No event with that address.")
        config = services.config_for(event)
        if config is None or not config.is_enabled:
            raise ApiError(404, "no_vote", f"{event.name} isn't running a community vote.")
        if config.access == VotingConfig.Access.LINK:
            if not secrets.compare_digest(request.GET.get("link", ""), config.link_token):
                raise ApiError(404, "bad_link", "This vote is by link. Pass the link's token as ?link=<token>.")
        voter = services.voter_for(request, event, config)
        if voter is None:
            if config.access == VotingConfig.Access.ACCOUNT:
                raise ApiError(401, "not_signed_in", "Send a session cookie. Sign in at /login/ to get one.")
            raise ApiError(401, "email_not_confirmed",
                           "Confirm an email address on the ballot page first; the session cookie then carries it.")

        if request.method == "POST":
            body = read_json(request)
            votes = body.get("votes", {})
            if not isinstance(votes, dict):
                raise ApiError(400, "invalid_ballot", 'Send {"votes": {"<project id>": <votes>}}.')
            saved, _ = services.save_ballot(event, voter, votes)
            if voter.kind == Ballot.Kind.LINK:
                services.remember(request, event, kind=Ballot.Kind.LINK, ballot=saved.key,
                                  order=voter.order_token)
            return JsonResponse(_state(event, config, voter, saved))
        return JsonResponse(_state(event, config, voter, services.find_ballot(event, voter)))
    except ApiError as err:
        return err.response()
    except WindowError as err:
        return ApiError(403, err.code, err.message).response()
    except services.VoteError as err:
        return ApiError(err.status, err.code, err.message).response()


def results(request, slug):
    """GET /api/events/<slug>/vote/results: the count, once it's public.

    403 results_hidden while voting is open or not yet reviewed, whoever asks,
    unless they organize the event. The body says which, and nothing else.
    """
    if request.method != "GET":
        return ApiError(405, "method_not_allowed", "Use GET.").response()
    event = Event.objects.filter(slug=slug).first()
    viewer = Viewer(request.user, event) if event else None
    if event is None or not viewer.can_view_event:
        return ApiError(404, "no_such_event", "No event with that address.").response()
    config = services.config_for(event)
    if config is None or not config.is_enabled:
        return ApiError(404, "no_vote", f"{event.name} isn't running a community vote.").response()
    state = services.results_state(event, config)
    if not services.can_see_results(viewer, event, config):
        detail = ("Results are hidden while voting is open." if state == "hidden"
                  else "Voting has closed; the organizers are reviewing the ballots before publishing.")
        return ApiError(403, "results_hidden", detail, state=state).response()
    tally = compute_tally(event, config)
    return JsonResponse({
        "event": event.slug,
        "state": state,
        "method": config.method,
        "ballots": tally["voted"],
        "results": [
            {"rank": line["rank"] if line["votes"] else None, "project": line["project"].pk,
             "title": line["project"].title, "team": line["project"].team.name,
             "votes": line["votes"], "backers": line["supporters"]}
            for line in tally["lines"]
        ],
    })

