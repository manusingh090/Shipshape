"""Duplicate and abuse detection. Everything here flags; nothing here punishes.

A crowd at a venue looks a lot like a ballot-stuffing script: one network,
one browser brand, votes for the popular project. So the detectors find the
patterns and say why, and an organizer decides, on the Integrity page. What
they decide (excluding a ballot, hiding a duplicate, removing spam) goes in
the audit log with their reason.

Ballot signals, each a reason shown to the organizer:

* burst: one of 5 or more new ballots from one network within 10 minutes;
* same device: the same network and browser as a ballot from someone else;
* copied: exactly the same votes as 3 or more other ballots;
* alias: an address that reaches the same inbox as another voter's
  (dots and +tags in Gmail, +tags anywhere). Email ballots now belong to the
  inbox, so this only fires between an email ballot and an account's, or on
  ballots cast before that change;
* fresh account: the account was made less than 30 minutes before voting.

One signal alone is often innocent; the page ranks ballots by how many they
have. Projects: the same repository or the same title handed in twice.
Comments: the same text posted on 3 or more projects. Judges: one judge's
normalized score for a project 1.5 or more from the mean of the rest of its
panel (judging/engine.panel_gaps; the trade-off is measured by
`engine.py --collusion`, JUDGING.md 11).
"""

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import timedelta
from urllib.parse import urlsplit

from projects.models import Comment, Project
from voting.models import Ballot

BURST_SIZE = 5
BURST_WINDOW = timedelta(minutes=10)
COPIES = 4            # a ballot and at least 3 identical others
FRESH_ACCOUNT = timedelta(minutes=30)
REPEATED_COMMENT = 3

GMAIL = {"gmail.com", "googlemail.com"}


def canonical_email(email):
    """The inbox an address really reaches: lowercased, +tag dropped, and for
    Gmail the dots too (a.b.c@gmail.com and abc+x@googlemail.com are one inbox)."""
    local, _, domain = (email or "").strip().lower().partition("@")
    local = local.split("+", 1)[0]
    if domain in GMAIL:
        local, domain = local.replace(".", ""), "gmail.com"
    return f"{local}@{domain}" if domain else local


@dataclass
class BallotFlag:
    ballot: Ballot
    reasons: list = field(default_factory=list)
    voter: str = ""

    @property
    def strength(self):
        return len(self.reasons)


def _voter_label(ballot):
    if ballot.email:
        return ballot.email
    if ballot.user_id:
        return ballot.user.email
    return f"open-link ballot #{ballot.pk}"


def ballot_flags(event):
    """Flagged ballots in this event, most reasons first."""
    ballots = list(Ballot.objects.filter(event=event).select_related("user").prefetch_related("entries__project")
                   .order_by("created_at", "id"))
    reasons = defaultdict(list)

    by_network = defaultdict(list)
    for b in ballots:
        if b.ip_hash:
            by_network[b.ip_hash].append(b)
    for group in by_network.values():
        start = 0
        for end in range(len(group)):
            while group[end].created_at - group[start].created_at > BURST_WINDOW:
                start += 1
            window = group[start:end + 1]
            if len(window) >= BURST_SIZE:
                for b in window:
                    text = f"one of {len(window)}+ new ballots from the same network within 10 minutes"
                    if not any(r.startswith("one of ") for r in reasons[b.pk]):
                        reasons[b.pk].append(text)

    by_device = defaultdict(list)
    for b in ballots:
        if b.ip_hash and b.user_agent:
            by_device[(b.ip_hash, b.user_agent)].append(b)
    for group in by_device.values():
        if len(group) >= 2:
            for b in group:
                reasons[b.pk].append(f"same network and browser as {len(group) - 1} other ballot"
                                     f"{'s' if len(group) > 2 else ''}")

    by_votes = defaultdict(list)
    for b in ballots:
        votes = tuple(sorted((e.project_id, e.votes) for e in b.entries.all()))
        if votes:
            by_votes[votes].append(b)
    for group in by_votes.values():
        if len(group) >= COPIES:
            for b in group:
                reasons[b.pk].append(f"exactly the same votes as {len(group) - 1} other ballots")

    by_inbox = defaultdict(list)
    for b in ballots:
        address = b.email or (b.user.email if b.user_id else "")
        if address:
            by_inbox[canonical_email(address)].append((b, address))
    for group in by_inbox.values():
        if len({address for _, address in group}) >= 2:
            for b, address in group:
                others = sorted({a for _, a in group if a != address})
                reasons[b.pk].append(f"{address} reaches the same inbox as {', '.join(others)}")

    for b in ballots:
        if b.user_id and b.created_at - b.user.date_joined < FRESH_ACCOUNT:
            minutes = max(0, int((b.created_at - b.user.date_joined).total_seconds() // 60))
            reasons[b.pk].append(f"account made {minutes} minute{'s' if minutes != 1 else ''} before voting")

    flags = [BallotFlag(ballot=b, reasons=reasons[b.pk], voter=_voter_label(b)) for b in ballots if reasons[b.pk]]
    flags.sort(key=lambda f: (-f.strength, f.ballot.created_at))
    return flags


def normalized_repo(url):
    if not url:
        return ""
    parts = urlsplit(url.strip().lower())
    host = parts.netloc.removeprefix("www.")
    path = parts.path.rstrip("/").removesuffix(".git")
    return f"{host}{path}" if host else ""


def normalized_title(title):
    return re.sub(r"[^a-z0-9]+", " ", (title or "").casefold()).strip()


def duplicate_projects(event):
    """Groups of listed or flagged submissions that share a repository or a
    title. A group already fully resolved (every other one flagged as a
    duplicate of the listed one) isn't shown again."""
    projects = list(Project.objects.filter(event=event, status=Project.Status.SUBMITTED)
                    .select_related("team", "duplicate_of").order_by("submitted_at", "id"))
    groups = []
    for label, key in (("the same repository", lambda p: normalized_repo(p.repo_url)),
                       ("the same title", lambda p: normalized_title(p.title))):
        buckets = defaultdict(list)
        for p in projects:
            k = key(p)
            if k:
                buckets[k].append(p)
        for members in buckets.values():
            listed = [p for p in members if p.duplicate_of_id is None]
            if len(members) >= 2 and len(listed) >= 2:
                groups.append({"why": label, "projects": members})
    seen, unique = set(), []
    for g in groups:
        ids = tuple(sorted(p.pk for p in g["projects"]))
        if ids not in seen:
            seen.add(ids)
            unique.append(g)
    return unique


def judge_disagreements(event):
    """Projects where one judge's view sits far from the rest of the panel,
    most extreme first. Each: {project, gap, judges: [{judge, mark, z, gap}]}."""
    from accounts.models import User
    from judging import engine
    from judging.results import compute_results

    results = compute_results(event)
    if not results.ready:
        return []
    groups = []
    for pid, gaps in engine.panel_gaps(results.z).items():
        worst = max(gaps.values(), key=abs)
        if abs(worst) >= engine.DISAGREEMENT:
            groups.append({"project_id": int(pid), "gap": worst, "judges": [
                {"judge_id": int(jid), "mark": results.raw[jid][pid], "z": results.z[jid][pid], "gap": g}
                for jid, g in sorted(gaps.items(), key=lambda item: -item[1])]})
    projects = Project.objects.select_related("team").in_bulk([g["project_id"] for g in groups])
    judges = User.objects.in_bulk({j["judge_id"] for g in groups for j in g["judges"]})
    for g in groups:
        g["project"] = projects[g["project_id"]]
        for j in g["judges"]:
            j["judge"] = judges[j["judge_id"]]
            j["flagged"] = abs(j["gap"]) >= engine.DISAGREEMENT
    return sorted(groups, key=lambda g: -abs(g["gap"]))


def normalized_comment(body):
    return re.sub(r"\s+", " ", (body or "").casefold()).strip()


def repeated_comments(event):
    """The same text (give or take case and spacing) on several projects."""
    rows = (Comment.objects.filter(project__event=event, removed_at__isnull=True)
            .select_related("author", "project").order_by("created_at"))
    buckets = defaultdict(list)
    for c in rows:
        text = normalized_comment(c.body)
        if len(text) >= 8:
            buckets[text].append(c)
    return [{"text": group[0].body, "comments": group}
            for group in buckets.values() if len({c.project_id for c in group}) >= REPEATED_COMMENT]
