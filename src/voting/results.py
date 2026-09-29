"""The count. Computed from the ballot entries on every request.

Only projects still listed in the gallery count: if an organizer flags one
as a duplicate after people voted for it, its votes drop out of the count
(they stay in the ballots and the CSV export). Ballots an organizer left out
after review (integrity/services.exclude_ballots) don't count either.
"""

from collections import defaultdict

from . import method
from .models import Ballot, BallotEntry
from .services import ballot_projects, config_for


def compute_tally(event, config=None):
    config = config or config_for(event)
    projects = list(ballot_projects(event))
    by_id = {p.pk: p for p in projects}

    ballots = defaultdict(dict)
    rows = (BallotEntry.objects.filter(ballot__event=event, ballot__excluded_at__isnull=True, project_id__in=by_id)
            .order_by("ballot_id", "project_id").values_list("ballot_id", "project_id", "votes"))
    for ballot_id, project_id, votes in rows:
        ballots[ballot_id][project_id] = votes

    lines = method.tally(ballots.values(), list(by_id))
    total_votes = sum(line.votes for line in lines)
    top = max((line.votes for line in lines), default=0)
    counted = [
        {"project": by_id[line.project], "rank": line.rank, "votes": line.votes, "supporters": line.supporters,
         "credits": line.credits,
         "share": round(100 * line.votes / total_votes, 1) if total_votes else 0.0,
         "bar": round(100 * line.votes / top) if top else 0}
        for line in lines
    ]

    all_ballots = Ballot.objects.filter(event=event, excluded_at__isnull=True)
    kinds = {kind: 0 for kind, _ in Ballot.Kind.choices}
    for kind in all_ballots.values_list("kind", flat=True):
        kinds[kind] += 1
    spent = [sum(method.cost(v) for v in b.values()) for b in ballots.values()]
    return {
        "lines": counted,
        "ballots": sum(kinds.values()),
        "voted": len(ballots),
        "by_kind": [(label, kinds[kind]) for kind, label in Ballot.Kind.choices if kinds[kind]],
        "total_votes": total_votes,
        "avg_credits": round(sum(spent) / len(spent), 1) if spent else None,
        "excluded": Ballot.objects.filter(event=event, excluded_at__isnull=False).count(),
        "avg_projects": round(sum(len(b) for b in ballots.values()) / len(ballots), 1) if ballots else None,
    }
