import os
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from accounts.demo import DEMO_LOGINS
from events.models import Event, EventRole
from events.seeding import Seeder, load_fixture
from projects.models import Project


class Command(BaseCommand):
    help = "Load fixtures.json (and demo accounts when DEMO_MODE=1). Safe to run on every boot."

    def add_arguments(self, parser):
        parser.add_argument("--fixtures", default=None, help="Path to fixtures.json")

    def handle(self, *args, **options):
        path = Path(options["fixtures"] or settings.FIXTURES_PATH)
        fixture = None
        if path.exists():
            fixture = load_fixture(path)
        else:
            self.stderr.write(f"fixtures file not found at {path}; skipping the fixture import")

        seeder = Seeder(demo=settings.DEMO_MODE, password=settings.DEMO_PASSWORD, out=self.stdout.write)
        event = seeder.run(fixture)

        if event is not None:
            projects = Project.objects.filter(event=event)
            dupes = projects.filter(duplicate_of__isnull=False).select_related("duplicate_of")
            dupe_note = ", ".join(f"{d.external_id or d.pk} repeats {d.duplicate_of.external_id or d.duplicate_of.pk}"
                                  for d in dupes)
            self.stdout.write(
                f"fixtures: {path.name} -> {event.name}: {event.tracks.count()} tracks, "
                f"{event.roles.filter(role=EventRole.Role.JUDGE).count()} judges, {event.teams.count()} teams, "
                f"{projects.count()} projects"
                + (f" ({dupes.count()} flagged duplicate: {dupe_note})" if dupes else "")
            )
        if event is not None:
            from judging.models import Assignment, Score

            scores = Score.objects.filter(event=event, submitted_at__isnull=False)
            pending = Assignment.objects.filter(event=event, score__isnull=True).count()
            self.stdout.write(
                f"judging: {event.criteria.count()} criteria, {scores.count()} submitted scores from "
                f"{scores.values('judge').distinct().count()} judges, {pending} review{'s' if pending != 1 else ''} "
                "assigned and not yet started"
            )
        for warning in seeder.warnings:
            self.stdout.write(f"note: {warning}")

        if settings.DEMO_MODE:
            self.print_logins()
        else:
            self.stdout.write("demo mode is off: no demo accounts or session cookies were created.")

    def print_logins(self):
        base = os.environ.get("PORTAL_URL", "http://localhost:8080")
        lines = ["", "seeded. test logins:"]
        for entry in DEMO_LOGINS:
            if entry["cookie"]:
                lines.append(f"  {entry['key']:<12} Cookie: session={entry['cookie']}")
        lines += [
            "",
            f"every seeded account signs in with the password: {settings.DEMO_PASSWORD}",
            "  organizer@example.org   organizer of both events",
            "  admin@example.org       platform admin",
            "  priya1@example.org      participant, captain of NorthKiln",
            "  tom@example.org         participant with no team yet",
            "  ...plus every judge and team member in fixtures.json",
            "",
            f"open events: {', '.join(e.name for e in Event.objects.all() if e.is_open) or 'none'}",
            f"portal: {base}",
            "",
        ]
        self.stdout.write("\n".join(lines))
