"""CSV builders for the community vote. Registered with the other stages in
judging/exports.py, so they share its formula defusing and its URL."""

from events.timeutil import iso_utc

from .models import BallotEntry
from .results import compute_tally
from .services import config_for


def community_votes(event):
    config = config_for(event)
    quadratic = config is not None and config.is_quadratic
    yield ["rank", "project_id", "external_id", "title", "team", "track", "votes", "backers",
           "credits_spent" if quadratic else "ballots", "share_percent"]
    if config is None:
        return
    for line in compute_tally(event, config)["lines"]:
        p = line["project"]
        yield [line["rank"] if line["votes"] else "", p.pk, p.external_id, p.title, p.team.name,
               p.track.name if p.track else "", line["votes"], line["supporters"],
               line["credits"] if quadratic else line["supporters"], line["share"]]


def ballots(event):
    """One row per project on each ballot. Organizers see who voted (email or
    account) because checking a suspicious burst of ballots needs it."""
    from integrity.detect import ballot_flags

    flags = {f.ballot.pk: "; ".join(f.reasons) for f in ballot_flags(event)}
    yield ["ballot_id", "kind", "voter", "created_at", "updated_at", "project_id", "title", "votes",
           "network", "listed", "counted", "excluded_reason", "flags"]
    rows = (BallotEntry.objects.filter(ballot__event=event)
            .select_related("ballot", "ballot__user", "project").order_by("ballot_id", "project_id"))
    for entry in rows:
        b, p = entry.ballot, entry.project
        voter = b.email or (b.user.email if b.user else "")
        listed = p.status == p.Status.SUBMITTED and p.duplicate_of_id is None
        yield [b.pk, b.kind, voter, iso_utc(b.created_at), iso_utc(b.updated_at), p.pk, p.title, entry.votes,
               b.ip_hash[:10], "yes" if listed else "no", "no" if b.excluded_at else "yes", b.excluded_reason,
               flags.get(b.pk, "")]
