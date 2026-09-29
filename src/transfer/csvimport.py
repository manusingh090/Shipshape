"""Bulk-adding teams and judges to an event from a spreadsheet.

The columns are the ones the portal's own CSV exports use (teams.csv and
judges.csv), so an export from one event or portal can be imported into
another. A minimal sheet works too:

    teams:  team, member_email, member_name (optional), role (optional: captain)
    judges: email, name (optional), tracks (optional: names, comma separated), all_tracks (optional: yes)

This adds to a running event, so the running event's rules hold, exactly as
if each person had been added by hand: rosters lock at the deadline, team
size limits, one team per person, staff don't compete, and organizers and
admins don't judge. Each row that breaks a rule is skipped and reported; the
rest go in. Like a whole-event import, a preview runs it all and rolls back.
"""

import csv
import io

from django.db import transaction

from accounts.models import User, normalize_email
from events.deadline import assert_team_changes_allowed, fresh
from events.models import EventRole, log_activity
from judging import staff
from judging.errors import JudgingError
from teams.models import Membership, Team

from .importer import TransferError

MAX_BYTES = 2 * 1024 * 1024
MAX_ROWS = 5000


class _Preview(Exception):
    pass


def rows(text):
    if len(text.encode("utf-8")) > MAX_BYTES:
        raise TransferError("That file is bigger than 2 MB.")
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    if not reader.fieldnames:
        raise TransferError("That file is empty.")
    reader.fieldnames = [(f or "").strip().lower() for f in reader.fieldnames]
    out = [{k: (v or "").strip() for k, v in row.items() if k} for row in reader]
    if len(out) > MAX_ROWS:
        raise TransferError(f"That's more than {MAX_ROWS} rows. Split it into smaller files.")
    return reader.fieldnames, out


def _person(email, name, report):
    email = normalize_email(email)
    if "@" not in email:
        return None
    user = User.objects.filter(email=email).first()
    if user is None:
        from django.contrib.auth.hashers import make_password

        user = User(email=email, name=(name or email.split("@")[0].replace(".", " ").title())[:120])
        user.password = make_password(None)
        user.save()
        report["new_accounts"] += 1
    return user


def _run(actor, event, work, preview):
    report = {"added": 0, "skipped": 0, "new_accounts": 0, "problems": []}
    try:
        with transaction.atomic():
            work(fresh(event), report)
            if preview:
                raise _Preview()
    except _Preview:
        pass
    return report


def _problem(report, line, text):
    report["skipped"] += 1
    if len(report["problems"]) < 200:
        report["problems"].append(f"Row {line}: {text}")


def import_teams(actor, event, text, preview=False):
    fields, data = rows(text)
    team_col = "team" if "team" in fields else "team_name" if "team_name" in fields else None
    email_col = "member_email" if "member_email" in fields else "email" if "email" in fields else None
    if not team_col or not email_col:
        raise TransferError("A teams sheet needs a team column and a member_email column (the teams.csv export has both).")

    def work(event, report):
        assert_team_changes_allowed(event)  # rosters lock at the deadline, for bulk as for one
        made = {}
        for line, row in enumerate(data, start=2):
            name, email = row.get(team_col, "")[:80], row.get(email_col, "")
            if not name or not email:
                _problem(report, line, "needs both a team and an email.")
                continue
            user = _person(email, row.get("member_name") or row.get("name"), report)
            if user is None:
                _problem(report, line, f"“{email}” isn't an email address.")
                continue
            if user.is_admin or EventRole.objects.filter(event=event, user=user).exists():
                _problem(report, line, f"{user.email} is staff here (organizer, judge or admin), and staff don't compete.")
                continue
            if Membership.objects.filter(event=event, user=user).exists():
                _problem(report, line, f"{user.email} is already on a team in this event.")
                continue
            key = (row.get("team_external_id") or name).lower()
            team = made.get(key) or Team.objects.filter(event=event, name=name).first()
            if team is None:
                team = Team.objects.create(event=event, name=name, created_by=actor)
            made[key] = team
            if team.memberships.count() >= event.max_team_size:
                _problem(report, line, f"{team.name} is full ({event.max_team_size} is the limit here).")
                continue
            captain = row.get("role", "").lower() == "captain" or not team.memberships.exists()
            if captain and team.memberships.filter(role=Membership.Role.CAPTAIN).exists():
                captain = False
            Membership.objects.create(team=team, user=user, event=event,
                                      role=Membership.Role.CAPTAIN if captain else Membership.Role.MEMBER)
            report["added"] += 1
        if report["added"]:
            log_activity(event, actor, "team.imported",
                         f"added {report['added']} people to {len(made)} team{'s' if len(made) != 1 else ''} from a spreadsheet")

    return _run(actor, event, work, preview)


def _tracks(event, text):
    by_name = {t.name.lower(): t for t in event.tracks.all()}
    found, unknown = [], []
    for part in [p.strip() for p in text.replace(";", ",").split(",") if p.strip()]:
        track = by_name.get(part.lower())
        (found if track else unknown).append(track or part)
    return found, unknown


def import_judges(actor, event, text, preview=False):
    fields, data = rows(text)
    if "email" not in fields:
        raise TransferError("A judges sheet needs an email column (the judges.csv export has one).")

    def work(event, report):
        for line, row in enumerate(data, start=2):
            user = _person(row.get("email", ""), row.get("name"), report)
            if user is None:
                _problem(report, line, f"“{row.get('email')}” isn't an email address.")
                continue
            everything = row.get("all_tracks", "").lower() in ("yes", "true", "1") or \
                row.get("tracks", "").strip().lower() == "every track"
            tracks, unknown = ([], []) if everything else _tracks(event, row.get("tracks", ""))
            if unknown:
                _problem(report, line, f"no track called {', '.join(repr(u) for u in unknown)} here; "
                                       f"{user.email} wasn't added.")
                continue
            try:
                staff.add_judge(actor, event, user, tracks, everything)
            except JudgingError as err:
                _problem(report, line, err.message)
                continue
            report["added"] += 1

    return _run(actor, event, work, preview)
