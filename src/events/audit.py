"""Reading the audit log without a database client: categories and search,
shared by the organizer's activity page and the admin's platform log."""

from django.db.models import Q

from .models import REFUSAL_VERBS

CATEGORIES = [
    ("", "Everything", None),
    ("refused", "Refused and rate-limited", Q(verb__endswith=".refused") | Q(verb__in=REFUSAL_VERBS)),
    ("integrity", "Anti-abuse decisions",
     Q(verb__in=["voting.ballot_excluded", "voting.ballot_restored", "project.flagged", "project.promoted",
                 "comment.removed"])),
    ("judging", "Judging", Q(verb__startswith="score.") | Q(verb__startswith="judge.") | Q(verb__startswith="judging.")),
    ("voting", "Community vote", Q(verb__startswith="voting.")),
    ("submissions", "Submissions, teams and comments",
     Q(verb__startswith="project.") | Q(verb__startswith="team.") | Q(verb__in=["comment.posted", "comment.deleted"])),
    ("settings", "Event settings, staff and webhooks",
     Q(verb__startswith="event.") | Q(verb__startswith="staff.") | Q(verb__startswith="webhook.")
     | Q(verb__startswith="track.") | Q(verb__startswith="prize.") | Q(verb__startswith="question.")),
    ("records", "Awards and certificates", Q(verb__startswith="award.") | Q(verb__startswith="record.")),
    ("accounts", "Accounts", Q(verb__startswith="admin.") | Q(verb__startswith="account.") | Q(verb="login.locked")),
]


def filtered(entries, category="", q=""):
    """(entries, the category key actually applied, the search text)."""
    rule = next((rule for key, _, rule in CATEGORIES if key == category), None)
    if rule is not None:
        entries = entries.filter(rule)
    else:
        category = ""
    q = (q or "").strip()[:100]
    if q:
        entries = entries.filter(Q(detail__icontains=q) | Q(actor__name__icontains=q) | Q(actor__email__icontains=q)
                                 | Q(verb__icontains=q))
    return entries, category, q


def category_links(keys=None):
    return [(key, label) for key, label, _ in CATEGORIES if keys is None or key in keys]
