"""CSV export at every stage of an event, organizer-only.

Each stage is one function returning a header and rows. Text that came from
users is defused against spreadsheet formula injection: a cell starting with
= + - @ (or a tab or carriage return) gets a leading apostrophe, so a project
called "=HYPERLINK(...)" is shown, not run, when an organizer opens the file
in Excel. Numbers are written as numbers and left alone.
"""

import csv
import io

from django.http import HttpResponse

from events.models import Activity
from events.timeutil import iso_utc
from projects.models import Comment, Project
from teams.models import Membership
from voting import exports as voting_exports

from .assigning import judge_roles
from .models import Assignment, Score, ScoreRevision
from .results import compute_progress, compute_results
from .staff import config_for, describe_tracks

STAGES = [
    ("teams", "Teams and members", "One row per team member, with their role on the team."),
    ("projects", "Submissions", "Every project: status, track, duplicate flag and review coverage."),
    ("judges", "Judges", "Judges, their tracks and how far through their queue they are."),
    ("assignments", "Assignments", "Who reviews what, which batch assigned it and whether it's done."),
    ("raw-scores", "Raw scores", "Every submitted score, one column per criterion plus the weighted total."),
    ("normalized-scores", "Normalized scores", "Raw, z and display score for every judge and project that counts."),
    ("final-rankings", "Final rankings", "Projects ranked by normalized score, with the raw ranking beside it."),
    ("score-history", "Score history", "Every save of every score, oldest first. Nothing is ever edited in place."),
    ("community-votes", "Community vote count", "Projects ranked by community votes, with backers and credits spent."),
    ("ballots", "Community ballots", "Every line of every ballot: who, when, which project, how many votes."),
    ("certificates", "Certificates and records", "Every record issued: who, what, its code, and whether it was revoked."),
    ("comments", "Comments", "Every comment on every project, removed ones included, with who removed them and why."),
    ("audit-log", "Audit log", "The event's whole activity log, including refused late edits."),
]
STAGE_NAMES = {key for key, _, _ in STAGES}


def defuse(value):
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        try:
            float(value)  # a plain number such as a negative z-score is data, not a formula
            return value
        except ValueError:
            return "'" + value
    return value


def _when(dt):
    return iso_utc(dt) if dt else ""


def _num(value, places=3):
    return "" if value is None else f"{value:.{places}f}"


def teams(event):
    yield ["team_id", "team_external_id", "team", "member_name", "member_email", "role", "joined_at"]
    for m in Membership.objects.filter(event=event).select_related("team", "user").order_by("team__name", "joined_at"):
        yield [m.team_id, m.team.external_id, m.team.name, m.user.display_name, m.user.email, m.role,
               _when(m.joined_at)]


def projects(event):
    target = config_for(event).reviews_per_project
    progress = {row.project.pk: row for row in compute_progress(event)["projects"]}
    yield ["project_id", "external_id", "title", "team", "track", "status", "submitted_at", "duplicate_of",
           "reviews_target", "reviews_assigned", "reviews_submitted"]
    for p in Project.objects.filter(event=event).select_related("team", "track", "duplicate_of").order_by("pk"):
        row = progress.get(p.pk)
        yield [p.pk, p.external_id, p.title, p.team.name, p.track.name if p.track else "", p.status,
               _when(p.submitted_at), p.duplicate_of.pk if p.duplicate_of else "",
               target if row else "", row.assigned if row else "", row.submitted if row else ""]


def judges(event):
    progress = {row.user.pk: row for row in compute_progress(event)["judges"]}
    yield ["judge_id", "external_id", "name", "email", "tracks", "all_tracks", "assigned", "submitted", "drafts",
           "status"]
    for role in judge_roles(event):
        row = progress[role.user_id]
        yield [role.user_id, role.user.external_id, role.user.display_name, role.user.email, describe_tracks(role),
               "yes" if role.all_tracks else "no", row.assigned, row.submitted, row.drafts, row.status]


def assignments(event):
    submitted = dict(Score.objects.filter(event=event).values_list("assignment_id", "submitted_at"))
    yield ["assignment_id", "batch", "judge_id", "judge", "project_id", "project", "track", "status", "assigned_at",
           "submitted_at"]
    rows = Assignment.objects.filter(event=event).select_related("batch", "judge", "project__track").order_by("pk")
    for a in rows:
        if a.pk not in submitted:
            status = "pending"
        elif submitted[a.pk] is None:
            status = "draft"
        else:
            status = "submitted"
        yield [a.pk, a.batch.label if a.batch else "manual", a.judge_id, a.judge.display_name, a.project_id,
               a.project.title, a.project.track.name if a.project.track else "", status, _when(a.created_at),
               _when(submitted.get(a.pk))]


def raw_scores(event):
    from .scoring import rubric_for, weighted

    criteria, rubric = rubric_for(event)
    yield (["judge_id", "judge_external_id", "judge", "project_id", "project_external_id", "project", "track"]
           + [c.key for c in criteria] + ["weighted_score", "counted", "comment", "submitted_at", "source"])
    scores = (Score.objects.filter(event=event, submitted_at__isnull=False)
              .select_related("judge", "project__track").prefetch_related("items").order_by("project_id", "judge_id"))
    for s in scores:
        marks = {item.criterion_id: item.value for item in s.items.all()}
        total = weighted(s, rubric)
        yield ([s.judge_id, s.judge.external_id, s.judge.display_name, s.project_id, s.project.external_id,
                s.project.title, s.project.track.name if s.project.track else ""]
               + [marks.get(c.pk, "") for c in criteria]
               + [_num(total), "yes" if s.project.is_listed and total is not None else "no", s.comment,
                  _when(s.submitted_at), s.source])


def normalized_scores(event):
    results = compute_results(event)
    yield ["judge_id", "project_id", "raw_score", "z_score", "display_score"]
    if not results.ready:
        return
    for judge_id, per_project in results.raw.items():
        for project_id, raw_value in per_project.items():
            yield [judge_id, project_id, _num(raw_value, 2), _num(results.z[judge_id][project_id]),
                   _num(results.display[judge_id][project_id], 2)]


def final_rankings(event):
    results = compute_results(event)
    yield ["rank", "project_id", "external_id", "title", "team", "track", "reviews", "informative_reviews",
           "raw_mean", "raw_rank", "normalized_z", "normalized_score", "movement"]
    for row in results.rows if results.ready else []:
        p = row.project
        yield [row.rank or "", p.pk, p.external_id, p.title, p.team.name, p.track.name if p.track else "",
               row.reviews, row.informative, _num(row.raw_mean), row.raw_rank or "", _num(row.z_mean),
               _num(row.display), "" if row.movement is None else row.movement]


def score_history(event):
    yield ["time", "judge_id", "judge", "project_id", "project", "submitted", "marks", "comment", "source"]
    rows = (ScoreRevision.objects.filter(event=event).select_related("judge", "project")
            .order_by("created_at", "id"))
    for r in rows:
        yield [_when(r.created_at), r.judge_id, r.judge.display_name, r.project_id, r.project.title,
               "yes" if r.submitted else "no", r.values_json, r.comment, r.source]


def certificates(event):
    from records.models import Record

    yield ["code", "kind", "recipient", "recipient_email", "issued_at", "issued_by", "revoked_at", "revoked_reason",
           "key_id"]
    for r in Record.objects.filter(event=event).select_related("user", "issued_by").order_by("issued_at", "id"):
        yield [r.code, r.kind, r.user.display_name if r.user else "", r.user.email if r.user else "",
               _when(r.issued_at), r.issued_by.display_name if r.issued_by else "", _when(r.revoked_at),
               r.revoked_reason, r.key_id]


def comments(event):
    yield ["comment_id", "project_id", "project", "team", "author", "author_email", "posted_at", "body",
           "removed_at", "removed_by", "removal_reason"]
    rows = (Comment.objects.filter(project__event=event)
            .select_related("project__team", "author", "removed_by").order_by("created_at", "id"))
    for c in rows:
        yield [c.pk, c.project_id, c.project.title, c.project.team.name, c.author.display_name, c.author.email,
               _when(c.created_at), c.body, _when(c.removed_at),
               c.removed_by.display_name if c.removed_by else "", c.removal_reason]


def audit_log(event):
    yield ["time", "actor", "action", "detail", "team", "project_id"]
    rows = Activity.objects.filter(event=event).select_related("actor", "team").order_by("created_at", "id")
    for a in rows:
        yield [_when(a.created_at), a.actor.display_name if a.actor else "", a.verb, a.detail,
               a.team.name if a.team else "", a.project_id or ""]


BUILDERS = {
    "teams": teams,
    "projects": projects,
    "judges": judges,
    "assignments": assignments,
    "raw-scores": raw_scores,
    "normalized-scores": normalized_scores,
    "final-rankings": final_rankings,
    "score-history": score_history,
    "community-votes": voting_exports.community_votes,
    "ballots": voting_exports.ballots,
    "certificates": certificates,
    "comments": comments,
    "audit-log": audit_log,
}


def render_csv(event, stage):
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    for row in BUILDERS[stage](event):
        writer.writerow([defuse(value) for value in row])
    return buffer.getvalue()


def csv_response(event, stage):
    response = HttpResponse(render_csv(event, stage), content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{event.slug}-{stage}.csv"'
    response["Cache-Control"] = "no-store"
    return response
