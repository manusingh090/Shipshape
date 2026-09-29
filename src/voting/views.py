"""The ballot, the gates in front of it, and the organizer's voting page.

Views stay thin: they work out which gate the request is at, then call the
services, which own every rule.
"""

import secrets

from django.conf import settings
from django.contrib import messages
from django.http import Http404, HttpResponseBadRequest
from django.shortcuts import redirect, render
from django.urls import reverse

from accounts.decorators import login_required
from accounts.throttle import client_ip
from events.access import get_event_for, require_organizer
from events.deadline import WindowError, voting_opens_at, voting_phase
from integrity.detect import ballot_flags

from . import services
from .forms import VotingConfigForm
from .models import Ballot, VotingConfig
from .results import compute_tally


def voting_event(request, slug):
    """The event and its voting settings, or 404 if it isn't running a vote."""
    event, viewer = get_event_for(request, slug)
    config = services.config_for(event)
    if config is None or not config.is_enabled:
        raise Http404()
    return event, viewer, config


def _context(event, config, **extra):
    credits, ceiling = services.limits(config)
    return {"event": event, "config": config, "phase": voting_phase(event, config),
            "opens_at": voting_opens_at(event, config), "credits": credits, "ceiling": ceiling,
            "cost_steps": [(n, n * n) for n in range(1, ceiling + 1)], **extra}


def _gate(request, event, config, gate, status=200, **extra):
    return render(request, "voting/gate.html", _context(event, config, gate=gate, **extra), status=status)


def ballot_link(request, slug, token):
    return ballot(request, slug, token=token)


def ballot(request, slug, token=None):
    event, viewer, config = voting_event(request, slug)
    if config.access == VotingConfig.Access.LINK:
        if token is None:
            return _gate(request, event, config, "link_needed")
        if not secrets.compare_digest(token, config.link_token):
            return _gate(request, event, config, "link_bad", status=404)
    elif token is not None:
        return redirect("voting:ballot", slug=slug)

    voter = services.voter_for(request, event, config)
    if voter is None:
        if config.access == VotingConfig.Access.ACCOUNT:
            return _gate(request, event, config, "sign_in")
        return _email_gate(request, event, config)

    error, submitted = None, None
    if request.method == "POST":
        op = request.POST.get("op", "save")
        if op == "forget" and voter.kind == Ballot.Kind.EMAIL:
            # Only for email ballots: switching address still means one
            # ballot per address. An open-link ballot has nothing else to
            # identify it, so forgetting it would just start a second one.
            services.forget(request, event)
            messages.info(request, "Signed out of this ballot. Your votes are kept.")
            return redirect(request.path)
        if op != "save":
            return HttpResponseBadRequest("Unknown operation.")
        submitted = _votes_from_post(request.POST, config)
        try:
            saved, spent = services.save_ballot(event, voter, submitted)
        except (WindowError, services.VoteError) as err:
            error = err.message
        else:
            if voter.kind == Ballot.Kind.LINK:
                services.remember(request, event, kind=Ballot.Kind.LINK, ballot=saved.key,
                                  order=voter.order_token)
            count = saved.entries.count()
            if not count:
                messages.success(request, "Ballot saved with no votes on it. You can come back and vote while voting is open.")
            elif config.is_quadratic:
                messages.success(request, f"Ballot saved: {count} project{'s' if count != 1 else ''}, "
                                          f"{spent} of {config.credits} credits.")
            else:
                messages.success(request, "Ballot saved.")
            return redirect(request.path)

    current = services.find_ballot(event, voter)
    votes = services.ballot_votes(current)
    if submitted is not None:
        votes = {pid: n for pid, n in _lenient(submitted).items()}
    refusal, own = services.eligibility(event, voter)
    rows = [{"project": p, "votes": votes.get(p.pk, 0), "cost": votes.get(p.pk, 0) ** 2, "own": p.pk in own}
            for p in services.ballot_for(event, config, voter)]
    spent = sum(n * n for n in votes.values()) if config.is_quadratic else len(votes)
    return render(request, "voting/ballot.html", _context(
        event, config, voter=voter, ballot=current, rows=rows, refusal=refusal, error=error, spent=spent,
        left=max(0, services.limits(config)[0] - spent),
        picked=next(iter(votes), None) if not config.is_quadratic else None,
    ))


def _votes_from_post(post, config):
    if config.is_quadratic:
        return {key[2:]: value for key, value in post.items() if key.startswith("p_")}
    pick = post.get("pick", "").strip()
    return {pick: 1} if pick else {}


def _lenient(raw):
    """Best-effort reading of a rejected ballot, so the form comes back as typed."""
    out = {}
    for key, value in raw.items():
        try:
            pid, n = int(key), int(value)
        except (TypeError, ValueError):
            continue
        if n > 0:
            out[pid] = n
    return out


def _confirm_url(request, event):
    return lambda token: request.build_absolute_uri(reverse("voting:email_confirm", args=[event.slug, token]))


def _signed_in(request):
    return request.user if request.user.is_authenticated else None


def _email_gate(request, event, config, error=None):
    account = _signed_in(request)
    # Signed in, the address is the account's; the form only shows it.
    email = account.email if account else ""
    if request.method == "POST" and request.POST.get("op") == "email":
        email = account.email if account else request.POST.get("email", "")
        try:
            _, link = services.request_email_pass(
                event, email, services.hash_ip(client_ip(request)), _confirm_url(request, event),
                signed_in=account,
            )
        except (WindowError, services.VoteError) as err:
            error = err.message
        else:
            return _gate(request, event, config, "email_sent", email=services.normalize_email(email),
                         demo_link=link if settings.DEMO_MODE else None,
                         outbox=not settings.EMAIL_HOST)
    return _gate(request, event, config, "email", email=email, error=error, account=account)


def email_confirm(request, slug, token):
    """GET shows a button; only the POST uses the link up. Mail scanners
    that pre-fetch links would otherwise burn every link before its owner
    clicked it."""
    event, viewer, config = voting_event(request, slug)
    if config.access != VotingConfig.Access.EMAIL:
        raise Http404()
    if request.method == "POST":
        try:
            email = services.redeem_email_pass(event, token, signed_in=_signed_in(request))
        except services.BadPass as err:
            return _email_gate(request, event, config, error=err.message)
        request.session.cycle_key()
        services.remember(request, event, kind=Ballot.Kind.EMAIL, email=email)
        messages.success(request, f"Confirmed: you're voting as {email}.")
        return redirect("voting:ballot", slug=slug)
    try:
        email_pass = services.check_email_pass(event, token)
        services.check_link_owner(email_pass, _signed_in(request))
    except services.BadPass as err:
        return _email_gate(request, event, config, error=err.message)
    return _gate(request, event, config, "confirm", email=email_pass.email)


def results(request, slug):
    """The public results page. Before voting closes and an organizer
    publishes, it says when results will come and shows no numbers at all,
    not even turnout. Organizers see a preview, clearly marked."""
    event, viewer, config = voting_event(request, slug)
    state = services.results_state(event, config)
    visible = services.can_see_results(viewer, event, config)
    return render(request, "voting/results.html", _context(
        event, config, viewer=viewer, state=state,
        tally=compute_tally(event, config) if visible else None,
        preview=visible and state != "published",
    ))


# ------------------------------------------------------------ organizers ----

@login_required
def manage(request, slug):
    event, viewer = get_event_for(request, slug)
    require_organizer(viewer)
    config = services.config_for(event, create=True)
    locked = services.voting_started(event)
    form = VotingConfigForm(instance=config, event=event, locked=locked)

    if request.method == "POST":
        op = request.POST.get("op")
        if op == "config":
            form = VotingConfigForm(request.POST, instance=config, event=event, locked=locked)
            if form.is_valid():
                config, taken_down = services.save_config(request.user, event, form)
                if taken_down:
                    messages.info(request, "Voting is no longer closed, so the published results were taken down.")
                messages.success(request, "Voting settings saved.")
                return redirect("voting:manage", slug=slug)
        elif op in ("publish", "unpublish", "close"):
            try:
                if op == "close":
                    services.close_now(event, request.user)
                    messages.success(request, "Voting is closed. Look over the ballots, then publish the results.")
                elif op == "publish":
                    services.publish_results(event, request.user)
                    messages.success(request, "Results published. Anyone can see them on the results page now.")
                else:
                    services.unpublish_results(event, request.user)
                    messages.success(request, "Results taken down. Only organizers can see the count.")
            except services.VoteError as err:
                messages.error(request, err.message)
            return redirect("voting:manage", slug=slug)
        elif op == "new_link":
            services.new_link(event, request.user)
            messages.success(request, "New voting link made. The old one no longer works.")
            return redirect("voting:manage", slug=slug)
        else:
            return HttpResponseBadRequest("Unknown operation.")

    link_url = request.build_absolute_uri(reverse("voting:ballot_link", args=[slug, config.link_token]))
    return render(request, "voting/manage.html", {
        **_context(event, config), "viewer": viewer, "section": "voting", "form": form, "locked": locked,
        "tally": compute_tally(event, config), "link_url": link_url,
        "results_state": services.results_state(event, config),
        "warning_ballots": sum(1 for f in ballot_flags(event) if f.strength >= 2 and f.ballot.excluded_at is None),
        "results_url": request.build_absolute_uri(reverse("voting:results", args=[slug])),
        "ballot_url": request.build_absolute_uri(reverse("voting:ballot", args=[slug])),
        "mail_host": settings.EMAIL_HOST, "outbox": str(settings.EMAIL_FILE_PATH) if not settings.EMAIL_HOST else "",
    })
