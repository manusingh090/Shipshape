"""The community vote (settings, publishing, the ballot, email links) and
the Integrity tab's decisions."""

from django.conf import settings
from django.urls import reverse

from accounts.throttle import client_ip
from integrity import detect, limits, services as review
from voting import api as voting_api
from voting import services as vote
from voting.forms import VotingConfigForm
from voting.models import Ballot
from voting.results import compute_tally

from .. import schema as sc
from .. import serializers as s
from ..core import ApiError, endpoint, ids, json_body
from .events import event_for, local_dates, patch_form


# What each endpoint takes and returns (api/schema.py), for the OpenAPI document.
RESULTS_STATE = sc.enum("hidden", "awaiting_review", "published",
                        description="hidden while voting is open; awaiting_review once it's closed; published after.")
VOTING = sc.obj(**sc.COMPONENTS["VotingConfig"]["properties"],
                locked=sc.boolean("A ballot is in, so access, method, credits, ceiling and order are fixed."),
                results_state=RESULTS_STATE, voting_link=sc.url("The open link (link-mode votes)."))
VOTING["required"] = sc.COMPONENTS["VotingConfig"]["required"] + ["locked", "results_state", "voting_link"]
VOTING_SAVED = sc.obj(**VOTING["properties"], results_taken_down=sc.boolean("Reopening voting unpublished the results."))
VOTING_SAVED["required"] = VOTING["required"] + ["results_taken_down"]
VOTING_BODY = sc.form_schema(VotingConfigForm, partial=True, event=True, locked=False, drop=("email_domains",), extra={
    "enabled": (sc.boolean("Another name for is_enabled."), False),
    "email_domains": (sc.any_of(sc.array(sc.string()), sc.string(), description="Email-gated votes: only these "
                                "domains, as a list or comma separated. Empty means any."), False)})
TALLY = sc.obj(ballots=sc.integer(), voted=sc.integer("Ballots with at least one vote."),
               excluded=sc.integer("Ballots an organizer left out."),
               results=sc.array(sc.obj(rank=sc.nullable(sc.integer()), project=sc.integer(), title=sc.string(),
                                       votes=sc.integer(), backers=sc.integer("Ballots that gave it a vote."),
                                       credits=sc.integer("Credits spent on it.")), "Every project, best first."))
BALLOT = sc.obj(
    event=sc.string(), access=sc.enum("link", "email", "account"), method=sc.enum("quadratic", "single"),
    credits=sc.integer("Your budget."), max_votes_per_project=sc.integer(),
    phase=sc.enum("off", "not_open", "open", "closed"), opens_at=sc.when(), closes_at=sc.nullable(sc.when()),
    can_vote=sc.boolean(), refusal=sc.nullable(sc.string("Why you can't vote, if you can't.")),
    ballot=sc.obj(votes=sc.mapping(sc.integer(), "Your votes, by project id."), credits_spent=sc.integer(),
                  updated_at=sc.nullable(sc.when())),
    projects=sc.array(sc.obj(id=sc.integer(), title=sc.string(), team=sc.string(), track=sc.nullable(sc.string()),
                             yours=sc.boolean("Your own team's: you can't vote for it.")),
                      "In your own order: shuffled for you, the same every time you come back."),
    order=sc.enum("shuffled", "alphabetical"))
BALLOT_BODY = sc.obj(votes=sc.mapping(sc.integer(minimum=0), "Votes by project id. On a quadratic ballot n votes cost "
                                                              "n*n credits; the total must fit the budget."))
LINK_QUERY = {"link": sc.string("Open-link votes: the link's token.")}
VOTE_RESULTS = sc.obj(event=sc.string(), state=RESULTS_STATE, method=sc.enum("quadratic", "single"),
                      ballots=sc.integer(),
                      results=sc.array(sc.obj(rank=sc.nullable(sc.integer()), project=sc.integer(), title=sc.string(),
                                              team=sc.string(), votes=sc.integer(), backers=sc.integer())))
LINK_SENT = sc.obj(sent=sc.boolean(), to=sc.string(),
                   demo_link=sc.optional(sc.url("Demo mode only (no mail server offline): the link that was mailed.")))
FLAGGED_BALLOT = sc.obj(ballot=sc.integer(), voter=sc.string("Who, as far as the ballot says."), counted=sc.boolean(),
                        created_at=sc.when(), network=sc.string("The start of the network's salted hash."),
                        votes=sc.mapping(sc.integer()), reasons=sc.array(sc.string()))
INTEGRITY = sc.obj(
    ballots=sc.array(FLAGGED_BALLOT, "Ballots with warning signs, most signs first."),
    excluded=sc.array(sc.obj(ballot=sc.integer(), reason=sc.string(), by=sc.nullable(sc.ref("Person")),
                             at=sc.nullable(sc.when()))),
    duplicate_projects=sc.array(sc.obj(why=sc.string(), projects=sc.array(sc.obj(
        id=sc.integer(), title=sc.string(), team=sc.string(), hidden=sc.boolean()))),
        "Listed projects with the same repository or title."),
    repeated_comments=sc.array(sc.obj(text=sc.string(), comments=sc.array(sc.integer()))),
    judge_disagreements=sc.array(sc.obj(project=sc.integer(), title=sc.string(), judges=sc.array(sc.obj(
        judge=sc.ref("Person"), mark=sc.number(), z=sc.number(), gap=sc.number("From the rest of the panel."),
        flagged=sc.boolean()))), "Projects where one judge stands 1.5 or more from the rest (JUDGING.md 11)."),
    limits=sc.array(sc.obj(scope=sc.string(), what=sc.string(), limit=sc.integer(), per=sc.string(), why=sc.string())))
REASON = sc.string("Why. Kept in the audit log.")


def _organizer(request, slug):
    event, _ = event_for(request, slug, organizer=True)
    return event


def _config_json(request, event):
    config = vote.config_for(event, create=True)
    data = s.voting_config(config)
    data["locked"] = vote.voting_started(event)
    data["results_state"] = vote.results_state(event, config)
    data["voting_link"] = request.build_absolute_uri(reverse("voting:ballot_link", args=[event.slug, config.link_token]))
    return data


@endpoint("GET", "events/<slug:slug>/voting", who="organizer", summary="The community vote's settings and state.",
          ui="Console, Community vote",
          returns=VOTING)
def get_voting(request, slug):
    return _config_json(request, _organizer(request, slug))


@endpoint("PATCH", "events/<slug:slug>/voting", who="organizer",
          summary="Change the vote. Access, method, budget, ceiling and order lock once a ballot is in.",
          ui="Console, Community vote, Save settings",
          body='{"is_enabled": true, "access": "link|email|account", "method": "quadratic|single", "closes_at": "...Z"}',
          request=VOTING_BODY, returns=VOTING_SAVED)
def update_voting(request, slug):
    event = _organizer(request, slug)
    config = vote.config_for(event, create=True)
    body = local_dates(json_body(request), event.tzinfo)
    if "enabled" in body:
        body["is_enabled"] = body.pop("enabled")
    if isinstance(body.get("email_domains"), list):
        body["email_domains"] = ", ".join(body["email_domains"])
    form = patch_form(VotingConfigForm, config, body, event=event, locked=vote.voting_started(event))
    _, taken_down = vote.save_config(request.user, event, form)
    return {**_config_json(request, event), "results_taken_down": taken_down}


@endpoint("POST", "events/<slug:slug>/voting/link", who="organizer",
          summary="Make a new open voting link; the old one stops working.", ui="Console, Community vote, Make a new link",
          returns=VOTING)
def new_link(request, slug):
    event = _organizer(request, slug)
    vote.new_link(event, request.user)
    return _config_json(request, event)


@endpoint("POST", "events/<slug:slug>/voting/close", who="organizer",
          summary="Close voting now, so its results can be reviewed and published.",
          ui="Console, Community vote, Close voting now", returns=VOTING)
def close_voting(request, slug):
    event = _organizer(request, slug)
    vote.close_now(event, request.user)
    return _config_json(request, event)


@endpoint("POST", "events/<slug:slug>/voting/publish", who="organizer",
          summary="Publish the results. Only after voting has closed.", ui="Console, Community vote, Publish the results",
          returns=VOTING)
def publish(request, slug):
    event = _organizer(request, slug)
    vote.publish_results(event, request.user)
    return _config_json(request, event)


@endpoint("POST", "events/<slug:slug>/voting/unpublish", who="organizer", summary="Take published results down.",
          ui="Console, Community vote, Take them down",
          returns=VOTING)
def unpublish(request, slug):
    event = _organizer(request, slug)
    vote.unpublish_results(event, request.user)
    return _config_json(request, event)


@endpoint("GET", "events/<slug:slug>/voting/tally", who="organizer",
          summary="The live count, which only organizers can see while voting is open.",
          ui="Console, Community vote, The count so far",
          returns=TALLY)
def tally(request, slug):
    event = _organizer(request, slug)
    t = compute_tally(event)
    return {"ballots": t["ballots"], "voted": t["voted"], "excluded": t["excluded"],
            "results": [{"rank": line["rank"] if line["votes"] else None, "project": line["project"].pk,
                         "title": line["project"].title, "votes": line["votes"], "backers": line["supporters"],
                         "credits": line["credits"]} for line in t["lines"]]}


def _pass(view):
    def run(request, **kwargs):
        return view(request, **kwargs)
    return run


get_ballot = endpoint("GET", "events/<slug:slug>/ballot", who="voter (per access mode)", auth=False,
                      summary="Your ballot, in your own shuffled order. Open-link votes need ?link=<token>.",
                      ui="Ballot page",
                      query=LINK_QUERY, returns=BALLOT)(_pass(voting_api.ballot))
save_ballot = endpoint("POST", "events/<slug:slug>/ballot", who="voter (per access mode)", auth=False,
                       summary="Save your ballot; it replaces the last one. Budget and ceiling checked on the server.",
                       ui="Ballot page, Save my ballot", body='{"votes": {"34": 3, "12": 1}}',
                       query=LINK_QUERY, request=BALLOT_BODY, returns=BALLOT)(_pass(voting_api.ballot))
vote_results = endpoint("GET", "events/<slug:slug>/vote/results", who="anyone once published; organizers always",
                        auth=False, summary="The community vote's results. 403 results_hidden until published.",
                        ui="Results page",
                        returns=VOTE_RESULTS)(_pass(voting_api.results))


@endpoint("POST", "events/<slug:slug>/ballot/email", who="anyone (email-gated votes)", auth=False,
          summary="Ask for a one-time voting link by email. Signed in, it's always your own address.",
          ui="Ballot page, Send me a voting link", body='{"email": "..."}',
          request=sc.obj(email=sc.optional(sc.string("Signed out: the address to vote with."))),
          returns=LINK_SENT)
def request_link(request, slug):
    event, _ = event_for(request, slug)
    signed_in = request.user if request.user.is_authenticated else None
    email = signed_in.email if signed_in else json_body(request).get("email", "")
    confirm = lambda t: request.build_absolute_uri(reverse("voting:email_confirm", args=[event.slug, t]))
    _, link = vote.request_email_pass(event, email, limits.network_key(client_ip(request)), confirm, signed_in=signed_in)
    data = {"sent": True, "to": vote.normalize_email(email)}
    if settings.DEMO_MODE:
        data["demo_link"] = link  # demo mode only: there's no mail server on an offline laptop
    return data


@endpoint("POST", "events/<slug:slug>/ballot/email/confirm", who="anyone with the emailed link", auth=False,
          summary="Use the emailed link's token. The session cookie in the reply then carries the confirmed address.",
          ui="Confirm and open my ballot", body='{"token": "..."}',
          request=sc.obj(token=sc.string()), returns=sc.obj(confirmed=sc.string("The address now voting.")))
def confirm_link(request, slug):
    event, _ = event_for(request, slug)
    signed_in = request.user if request.user.is_authenticated else None
    email = vote.redeem_email_pass(event, str(json_body(request).get("token", "")), signed_in=signed_in)
    request.session.cycle_key()
    vote.remember(request, event, kind=Ballot.Kind.EMAIL, email=email)
    return {"confirmed": email}


@endpoint("POST", "events/<slug:slug>/ballot/forget", who="email voter, signed out", auth=False,
          summary="Stop voting as the confirmed address in this session. The ballot is kept.",
          ui="Ballot page, Use a different address",
          returns=sc.obj(forgotten=sc.boolean()))
def forget(request, slug):
    event, _ = event_for(request, slug)
    if request.user.is_authenticated:
        raise ApiError(400, "invalid", "Signed in, you vote with your own address. Sign out to use another.")
    vote.forget(request, event)
    return {"forgotten": True}


# ---------------------------------------------------------------- integrity

@endpoint("GET", "events/<slug:slug>/integrity", who="organizer",
          summary="What the anti-abuse checks found: flagged ballots, duplicates, repeated comments, judges far "
                  "from their panel, the limits.",
          ui="Console, Integrity",
          returns=INTEGRITY)
def integrity(request, slug):
    event = _organizer(request, slug)
    flags = detect.ballot_flags(event)
    return {
        "ballots": [{"ballot": f.ballot.pk, "voter": f.voter, "counted": f.ballot.excluded_at is None,
                     "created_at": s.when(f.ballot.created_at), "network": f.ballot.ip_hash[:10],
                     "votes": {str(e.project_id): e.votes for e in f.ballot.entries.all()}, "reasons": f.reasons}
                    for f in flags],
        "excluded": [{"ballot": b.pk, "reason": b.excluded_reason, "by": s.person(b.excluded_by),
                      "at": s.when(b.excluded_at)}
                     for b in Ballot.objects.filter(event=event, excluded_at__isnull=False).select_related("excluded_by")],
        "duplicate_projects": [{"why": g["why"], "projects": [{"id": p.pk, "title": p.title, "team": p.team.name,
                                                                "hidden": p.duplicate_of_id is not None}
                                                               for p in g["projects"]]}
                               for g in detect.duplicate_projects(event)],
        "repeated_comments": [{"text": g["text"], "comments": [c.pk for c in g["comments"]]}
                              for g in detect.repeated_comments(event)],
        "judge_disagreements": [{"project": g["project_id"], "title": g["project"].title,
                                 "judges": [{"judge": s.person(j["judge"]), "mark": round(j["mark"], 3),
                                             "z": round(j["z"], 3), "gap": round(j["gap"], 3), "flagged": j["flagged"]}
                                            for j in g["judges"]]}
                                for g in detect.judge_disagreements(event)],
        "limits": [{"scope": l.scope, "what": l.what, "limit": l.limit, "per": l.window_label, "why": l.why}
                   for l in limits.LIMITS.values()],
    }


@endpoint("POST", "events/<slug:slug>/integrity/ballots/exclude", who="organizer",
          summary="Leave ballots out of the count, with a reason. Reversible.",
          ui="Console, Integrity, Leave the picked ballots out", body='{"ballots": [5, 6], "reason": "..."}',
          request=sc.obj(ballots=sc.array(sc.integer()), reason=REASON),
          returns=sc.obj(excluded=sc.integer()))
def exclude(request, slug):
    event = _organizer(request, slug)
    body = json_body(request)
    return {"excluded": review.exclude_ballots(event, request.user, ids(body.get("ballots")), body.get("reason", ""))}


@endpoint("POST", "events/<slug:slug>/integrity/ballots/<int:pk>/restore", who="organizer",
          summary="Count a left-out ballot again.", ui="Console, Integrity, Count it again",
          returns=sc.obj(restored=sc.integer()))
def restore(request, slug, pk):
    event = _organizer(request, slug)
    return {"restored": review.restore_ballot(event, request.user, pk).pk}


@endpoint("POST", "events/<slug:slug>/integrity/projects/<int:pk>/hide", who="organizer",
          summary="Hide a submission as a duplicate of another, with a reason.", ui="Console, Integrity, Hide it",
          body='{"original": 12, "reason": "..."}',
          request=sc.obj(original=sc.integer("The project it duplicates."), reason=REASON),
          returns=sc.obj(hidden=sc.integer(), duplicate_of=sc.integer()))
def hide(request, slug, pk):
    event = _organizer(request, slug)
    body = json_body(request)
    p = review.hide_duplicate(event, request.user, pk, body.get("original"), body.get("reason", ""))
    return {"hidden": p.pk, "duplicate_of": p.duplicate_of_id}


@endpoint("POST", "events/<slug:slug>/integrity/comments/remove", who="organizer",
          summary="Remove several comments at once, with a reason.", ui="Console, Integrity, Remove the ticked comments",
          body='{"comments": [3, 4], "reason": "spam"}',
          request=sc.obj(comments=sc.array(sc.integer()), reason=REASON),
          returns=sc.obj(removed=sc.integer()))
def remove_comments(request, slug):
    event = _organizer(request, slug)
    body = json_body(request)
    return {"removed": review.remove_repeated(event, request.user, ids(body.get("comments")), body.get("reason", ""))}
