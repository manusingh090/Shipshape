"""Every rule about community voting lives here.

The ballot page and the JSON API both call save_ballot, so they can't drift
apart. It follows the same shape as a submission or a score: one
transaction, the event and the voting settings re-read, the window checked
with the server's clock, then the write.

Who a voter is comes from the access mode:

* open link: the browser session that opened the organizer's secret link
  (plus the account, if they happen to be signed in);
* email-gated: an address confirmed by a one-time link;
* signed in: the account.
"""

import hashlib
import hmac
import secrets
import smtplib
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.mail import send_mail
from django.core.validators import validate_email
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.db.models.functions import Lower
from django.utils import timezone

from accounts.models import User, normalize_email
from accounts.throttle import client_ip
from events.access import Viewer
from events.deadline import assert_voting_open, fresh, voting_phase
from events.models import log_activity
from events.timeutil import format_dt
from integrity import limits as rate_limits
from integrity.detect import GMAIL, canonical_email
from projects.models import Project
from teams.models import Membership

from . import method
from .models import Ballot, BallotEntry, EmailPass, VotingConfig, hash_token

SESSION_KEY = "voting"
PASS_WINDOW = timedelta(minutes=15)   # the vote.link_address limit's window


class VoteError(Exception):
    """A rule said no. The message is written for the voter."""

    status = 400
    code = "vote_error"

    def __init__(self, message):
        super().__init__(message)
        self.message = message


class NotEligible(VoteError):
    status = 403
    code = "not_eligible"


class RateLimited(VoteError):
    status = 429
    code = "slow_down"


class BadPass(VoteError):
    status = 400
    code = "link_not_valid"


# ------------------------------------------------------------- settings ----

def config_for(event, create=False):
    if create:
        return VotingConfig.objects.get_or_create(event=event)[0]
    return VotingConfig.objects.filter(event=event).first()


def voting_started(event):
    """Once a ballot exists, the access mode, method and budget are fixed, so
    every ballot in the count was cast under the same rules."""
    return Ballot.objects.filter(event=event).exists()


def ballot_projects(event):
    """The projects on the ballot: everything listed in the gallery for this
    event, A to Z. What a voter sees is ballot_for(), which reorders them."""
    return (Project.objects.filter(event=event, status=Project.Status.SUBMITTED, duplicate_of__isnull=True)
            .select_related("team", "track").order_by(Lower("title"), "id"))


def order_seed(event, voter):
    """A per-voter seed, stable for as long as the voter is the same voter:
    their account, their confirmed address, or (open link) a token kept in
    their session. Mixed with the site secret, so nobody can work out which
    projects another voter sees first."""
    if voter.kind == Ballot.Kind.ACCOUNT:
        who = f"account:{voter.user.pk}"
    elif voter.kind == Ballot.Kind.EMAIL:
        who = f"email:{voter.email}"
    else:
        who = f"link:{voter.order_token}"
    return hmac.new(settings.SECRET_KEY.encode(), f"ballot-order:{event.pk}:{who}".encode(),
                    hashlib.sha256).digest()


def ballot_for(event, config, voter):
    """The projects in the order this voter sees them.

    Shuffled (the default), every voter gets their own order, the same each
    time they come back, so no project collects votes just for sitting near
    the top. One order for everyone, A to Z or a single shuffle, doesn't do
    that: it only decides who gets the good spot (JUDGING.md, section 10.5).
    """
    projects = list(ballot_projects(event))
    if config.order != VotingConfig.Order.SHUFFLED or voter is None:
        return projects
    projects.sort(key=lambda p: p.pk)  # a base order that doesn't depend on titles
    return method.shuffled(projects, order_seed(event, voter))


def limits(config):
    """(credits, most votes on one project) for this ballot."""
    if config.is_quadratic:
        return config.credits, method.max_votes_per_project(config.credits, config.max_votes)
    return 1, 1


# ------------------------------------------------------------ identity ----

@dataclass
class Voter:
    kind: str
    user: object = None       # the account the ballot belongs to (account and link ballots)
    email: str = ""           # the confirmed address (email ballots)
    ballot_key: str = ""      # the session's ballot (link ballots)
    ip_hash: str = ""
    user_agent: str = ""
    signed_in: object = None  # whoever is signed in, whatever the mode; only used for the rules
    order_token: str = ""     # seeds an open-link voter's ballot order (kept in their session)


def hash_ip(ip):
    return rate_limits.network_key(ip)


def _session_entry(request, event):
    return (request.session.get(SESSION_KEY) or {}).get(str(event.pk)) or {}


def remember(request, event, **entry):
    store = dict(request.session.get(SESSION_KEY) or {})
    store[str(event.pk)] = entry
    request.session[SESSION_KEY] = store


def forget(request, event):
    store = dict(request.session.get(SESSION_KEY) or {})
    store.pop(str(event.pk), None)
    request.session[SESSION_KEY] = store


def voter_for(request, event, config):
    """Who is voting, as far as this access mode is concerned, or None if the
    request hasn't got past the gate yet (not signed in, email not confirmed).
    The open link's own check (the token in the address) is the view's job."""
    user = request.user if request.user.is_authenticated else None
    base = {"ip_hash": hash_ip(client_ip(request)),
            "user_agent": request.headers.get("User-Agent", "")[:200], "signed_in": user}
    entry = _session_entry(request, event)
    if config.access == VotingConfig.Access.ACCOUNT:
        return Voter(kind=Ballot.Kind.ACCOUNT, user=user, **base) if user else None
    if config.access == VotingConfig.Access.EMAIL:
        confirmed = entry.get("email") if entry.get("kind") == Ballot.Kind.EMAIL else ""
        # Signed in, you vote as your own account's address and no other: an
        # address confirmed earlier in this browser doesn't carry over to a
        # different account.
        if not confirmed or (user is not None and confirmed != user.email):
            return None
        return Voter(kind=Ballot.Kind.EMAIL, email=confirmed, **base)
    link_entry = entry if entry.get("kind") == Ballot.Kind.LINK else {}
    token = link_entry.get("order", "")
    if not token:
        # First visit: give this browser its own ballot order, kept with the rest.
        token = secrets.token_urlsafe(16)
        remember(request, event, **{**link_entry, "kind": Ballot.Kind.LINK, "order": token})
    return Voter(kind=Ballot.Kind.LINK, user=user, ballot_key=link_entry.get("ballot", ""),
                 order_token=token, **base)


def inbox(email):
    """The mailbox an address reaches, which is what an email ballot belongs
    to. ada+1@x.org and ada+2@x.org, or a.da@gmail.com and ada@gmail.com, are
    one person with one ballot. Mail still goes to the address as typed."""
    return canonical_email(email) if email else ""


def voter_key(voter):
    """Names this voter for rate limits: their account, address or ballot."""
    if voter.user is not None:
        return f"user:{voter.user.pk}"
    if voter.email:
        return f"email:{inbox(voter.email)}"
    return f"link:{voter.ballot_key or voter.order_token or voter.ip_hash}"


def find_ballot(event, voter):
    if voter is None:
        return None
    qs = Ballot.objects.filter(event=event, kind=voter.kind)
    if voter.kind == Ballot.Kind.ACCOUNT:
        return qs.filter(user=voter.user).first()
    if voter.kind == Ballot.Kind.EMAIL:
        # Ballots from before inboxes were used keep the address as typed.
        return qs.filter(email__in={inbox(voter.email), voter.email}).order_by("pk").first()
    if voter.user is not None:
        found = qs.filter(user=voter.user).first()
        if found:
            return found
    return qs.filter(key=voter.ballot_key).first() if voter.ballot_key else None


def _accounts_for(voter):
    """Every account this voter is known to be: the ballot's owner, whoever is
    signed in, and the account behind a confirmed email address."""
    accounts = {a for a in (voter.user, voter.signed_in) if a is not None}
    if voter.email:
        mailbox = inbox(voter.email)
        domain = mailbox.partition("@")[2]
        domains = GMAIL if domain in GMAIL else {domain}
        # Any account whose address reaches the same inbox is the same person.
        by_domain = Q()
        for d in domains:
            by_domain |= Q(email__iendswith="@" + d)
        accounts.update(u for u in User.objects.filter(by_domain) if inbox(u.email) == mailbox)
        accounts.update(User.objects.filter(email=voter.email))
    return accounts


def eligibility(event, voter):
    """(reason this voter can't vote at all or None, ids of their own team's projects).

    Organizers and admins can't vote: they can see the count as it happens.
    Nobody can vote for their own team. Both rules apply to every account the
    voter is known to be, so an organizer can't slip in through an email
    ballot or an open link while signed in, and a participant can't back
    their own project from an address they confirmed.
    """
    accounts = _accounts_for(voter) if voter else set()
    for account in accounts:
        if Viewer(account, event).is_organizer:
            return ("Organizers and admins can't vote in the community vote: "
                    "they can see the count as it comes in."), set()
    if not accounts:
        return None, set()
    own = set(Project.objects.filter(
        event=event, team__in=Membership.objects.filter(event=event, user__in=accounts).values("team")
    ).values_list("pk", flat=True))
    return None, own


# -------------------------------------------------------------- ballots ----

def clean_votes(raw):
    """{project id: votes} from form or JSON input, zeros dropped."""
    clean = {}
    for key, value in raw.items():
        try:
            project_id = int(key)
        except (TypeError, ValueError):
            raise VoteError("That ballot names a project that isn't on it.")
        if value in (None, ""):
            continue
        if isinstance(value, bool):
            raise VoteError("Votes are whole numbers.")
        try:
            votes = int(value)
        except (TypeError, ValueError):
            raise VoteError("Votes are whole numbers.")
        if isinstance(value, float) and value != votes:
            raise VoteError("Votes are whole numbers.")
        if votes < 0:
            raise VoteError("Votes can't be negative.")
        if votes:
            clean[project_id] = votes
    return clean


def save_ballot(event, voter, raw_votes):
    """Replace this voter's ballot with raw_votes. Returns (ballot, credits spent).

    An empty ballot is allowed: it withdraws every vote but keeps the ballot,
    so the voter can come back and vote again while voting is open.
    """
    if voter is None:
        raise NotEligible("Get past the gate first: sign in, or confirm your email.")
    # Rate limits before the transaction, so a refusal is logged.
    who = voter_key(voter)
    rate_limits.check("vote.save", who, "That's a lot of ballot saves in a minute. Wait a moment.",
                 exc=RateLimited, event=event, actor=voter.signed_in)
    if find_ballot(event, voter) is None and voter.ip_hash:
        hour_ago = timezone.now() - timedelta(hours=1)
        rate_limits.check("vote.new_ballot", voter.ip_hash,
                     "A lot of new ballots have come from this network in the last hour. Ask the organizers.",
                     count=Ballot.objects.filter(event=event, ip_hash=voter.ip_hash, created_at__gte=hour_ago).count(),
                     exc=RateLimited, event=event, actor=voter.signed_in, detail=f"network {voter.ip_hash[:10]}")
    rate_limits.record("vote.save", who)
    with transaction.atomic():
        event = fresh(event)
        config = VotingConfig.objects.select_for_update().filter(event=event).first()
        assert_voting_open(event, config)
        refusal, own = eligibility(event, voter)
        if refusal:
            raise NotEligible(refusal)

        votes = clean_votes(raw_votes)
        listed = set(ballot_projects(event).values_list("pk", flat=True))
        if set(votes) - listed:
            raise VoteError("That ballot names a project that isn't on it.")
        if set(votes) & own:
            raise NotEligible("You can't vote for your own team's project.")
        try:
            spent = method.check(config.method, votes, config.credits, config.max_votes)
        except method.BallotInvalid as err:
            raise VoteError(str(err))

        ballot = find_ballot(event, voter)
        now = timezone.now()
        if ballot is None:
            ballot = Ballot(event=event, kind=voter.kind, user=voter.user, email=inbox(voter.email),
                            ip_hash=voter.ip_hash, user_agent=voter.user_agent, created_at=now)
        ballot.updated_at = now
        try:
            with transaction.atomic():
                ballot.save()
        except IntegrityError:
            # Two tabs racing to create the same account's or email's ballot.
            raise VoteError("Your ballot was saved from another tab a moment ago. Reload and try again.")
        ballot.entries.all().delete()
        BallotEntry.objects.bulk_create(
            [BallotEntry(ballot=ballot, project_id=pid, votes=n) for pid, n in sorted(votes.items())]
        )
    return ballot, spent


def ballot_votes(ballot):
    return dict(ballot.entries.values_list("project_id", "votes")) if ballot else {}


# ------------------------------------------------------------ email pass ----

def allowed_email(config, email):
    domains = config.domain_list
    return not domains or email.rsplit("@", 1)[-1] in domains


def request_email_pass(event, email, ip_hash, confirm_url, signed_in=None):
    """Mail a one-time link to email. confirm_url(token) builds its address.

    Returns (the pass, the link). The link is returned so demo mode can show
    it on screen: an offline laptop has no mail server to hand it to.
    """
    email = normalize_email(email)
    if signed_in is not None and email != signed_in.email:
        raise VoteError(f"You're signed in as {signed_in.email}, so that's the address you vote with. "
                        "Sign out first to vote with another one.")
    try:
        validate_email(email)
    except ValidationError:
        raise VoteError("That doesn't look like an email address.")
    # Rate limits before the transaction, so a refusal is logged.
    now = timezone.now()
    rate_limits.check("vote.link_address", email,
                 "We've sent that address a few links already. Check the inbox, or try again in 15 minutes.",
                 count=sum(1 for sent in EmailPass.objects.filter(event=event, created_at__gte=now - PASS_WINDOW)
                           .values_list("email", flat=True) if inbox(sent) == inbox(email)),
                 exc=RateLimited, event=event, actor=signed_in, detail=email)
    rate_limits.check("vote.link_network", ip_hash, "Too many links asked for from this network. Try again in an hour.",
                 count=EmailPass.objects.filter(ip_hash=ip_hash, created_at__gte=now - timedelta(hours=1)).count(),
                 exc=RateLimited, event=event, actor=signed_in, detail=f"network {ip_hash[:10]}")
    with transaction.atomic():
        event = fresh(event)
        config = VotingConfig.objects.filter(event=event).first()
        assert_voting_open(event, config)
        if config.access != VotingConfig.Access.EMAIL:
            raise VoteError("This vote doesn't use email links.")
        if not allowed_email(config, email):
            raise NotEligible(f"This vote is open to addresses at {', '.join(config.domain_list)}.")
        refusal, _ = eligibility(event, Voter(kind=Ballot.Kind.EMAIL, email=email, signed_in=signed_in))
        if refusal:
            raise NotEligible(refusal)

        now = timezone.now()
        token = secrets.token_urlsafe(32)
        email_pass = EmailPass.objects.create(event=event, email=email, token_hash=hash_token(token),
                                              ip_hash=ip_hash, created_at=now)
    link = confirm_url(token)
    closes = f" Voting closes {format_dt(config.closes_at, event.tzinfo)}." if config.closes_at else ""
    try:
        _send_pass(event, email, link, closes)
    except (smtplib.SMTPException, OSError):
        raise VoteError("We couldn't send the email just now. Try again in a minute, or tell the organizers.")
    return email_pass, link


def _send_pass(event, email, link, closes):
    send_mail(
        subject=f"Your ballot for {event.name}",
        message=(
            f"Someone (hopefully you) asked to vote in the {event.name} community vote with this address.\n\n"
            f"Open this link to get to your ballot. It works once, for the next 30 minutes:\n\n{link}\n\n"
            f"You can change your votes until voting closes by asking for a new link.{closes}\n\n"
            "If this wasn't you, ignore this message. Nobody can vote with your address without it.\n"
        ),
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[email],
        fail_silently=False,
    )


def check_email_pass(event, token):
    """The pass for this token if it can still be used, else raise BadPass."""
    email_pass = EmailPass.objects.filter(event=event, token_hash=hash_token(token or "")).first()
    if email_pass is None:
        raise BadPass("That link isn't valid. Ask for a new one below.")
    if email_pass.used_at:
        raise BadPass("That link has been used already. Links work once; ask for a new one below.")
    if timezone.now() >= email_pass.expires_at:
        raise BadPass("That link has expired. Links last 30 minutes; ask for a new one below.")
    return email_pass


def check_link_owner(email_pass, signed_in):
    """A signed-in person can only use a link for their own address."""
    if signed_in is not None and email_pass.email != signed_in.email:
        raise BadPass(f"This link is for {email_pass.email}, but you're signed in as {signed_in.email}. "
                      "Sign out to use it, or ask for a link for your own address. The link still works.")


def redeem_email_pass(event, token, signed_in=None):
    """Use the link up and return the address it confirms."""
    with transaction.atomic():
        email_pass = check_email_pass(event, token)
        check_link_owner(email_pass, signed_in)
        # Mark used with a guarded update, so two clicks can't both succeed.
        claimed = EmailPass.objects.filter(pk=email_pass.pk, used_at__isnull=True).update(used_at=timezone.now())
        if not claimed:
            raise BadPass("That link has been used already. Links work once; ask for a new one below.")
    return email_pass.email


# -------------------------------------------------------------- console ----

def new_link(event, actor):
    config = config_for(event, create=True)
    config.link_token = secrets.token_urlsafe(24)
    config.save(update_fields=["link_token", "updated_at"])
    log_activity(event, actor, "voting.link", "made a new voting link; the old one stopped working")
    return config


# -------------------------------------------------------------- results ----
# Organizers see the count all the time. Everyone else sees nothing, not even
# how many ballots are in, until voting has closed and an organizer has
# reviewed the ballots and published. Every page, the API and the exports ask
# this one function.

def results_state(event, config):
    """hidden (voting not closed), awaiting_review (closed, not published) or published."""
    if voting_phase(event, config) != "closed":
        return "hidden"
    if config.results_published_at is None:
        return "awaiting_review"
    return "published"


def can_see_results(viewer, event, config):
    if config is None or not config.is_enabled:
        return False
    return viewer.is_organizer or results_state(event, config) == "published"


def close_now(event, actor):
    """End voting at this moment, so its results can be reviewed and published."""
    with transaction.atomic():
        event = fresh(event)
        config = VotingConfig.objects.select_for_update().get(event=event)
        if voting_phase(event, config) != "open":
            raise VoteError("Voting isn't open, so there's nothing to close.")
        old = config.closes_at
        config.closes_at = timezone.now()
        config.save(update_fields=["closes_at", "updated_at"])
    log_activity(event, actor, "voting.closed", "closed voting early" + (
        f" (it was due to close {format_dt(old, event.tzinfo)})" if old else ""))
    return config


def publish_results(event, actor):
    with transaction.atomic():
        event = fresh(event)
        config = VotingConfig.objects.select_for_update().get(event=event)
        if voting_phase(event, config) != "closed":
            raise VoteError("Results can only be published once voting has closed.")
        config.results_published_at = timezone.now()
        config.results_published_by = actor
        config.save(update_fields=["results_published_at", "results_published_by", "updated_at"])
    log_activity(event, actor, "voting.published", "published the community vote results")
    return config


def unpublish_results(event, actor):
    config = config_for(event, create=True)
    config.results_published_at = None
    config.results_published_by = None
    config.save(update_fields=["results_published_at", "results_published_by", "updated_at"])
    log_activity(event, actor, "voting.unpublished", "took the community vote results down")
    return config


def save_config(actor, event, form):
    """form: a valid VotingConfigForm. If the settings reopen voting, results
    that were published come down, because they'd be a live count. Returns
    (config, whether results were taken down)."""
    changed = ", ".join(form.fields[name].label.lower() for name in form.changed_data)
    config = form.save()
    if changed:
        log_activity(event, actor, "voting.config", f"changed the community vote ({changed})")
    if config.results_published_at and voting_phase(event, config) != "closed":
        unpublish_results(event, actor)
        return config, True
    return config, False

