"""Show what normalization does to a real event, and why.

    python manage.py normalization_proof                    the fixture event
    python manage.py normalization_proof --trials 1000      plus the simulation
"""

import statistics

from django.core.management.base import BaseCommand, CommandError

from events.models import Event
from judging import engine
from judging.results import compute_results


class Command(BaseCommand):
    help = "Raw versus normalized ranking for an event, with the biggest moves explained."

    def add_arguments(self, parser):
        parser.add_argument("--event", default="sample-hack-2026", help="Event slug")
        parser.add_argument("--movers", type=int, default=4, help="How many big moves to explain")
        parser.add_argument("--trials", type=int, default=0, help="Also run the Monte Carlo proof")

    def handle(self, *args, **options):
        event = Event.objects.filter(slug=options["event"]).first()
        if event is None:
            raise CommandError(f"No event called {options['event']!r}.")
        results = compute_results(event)
        if not results.ready:
            raise CommandError(results.reason)
        out = self.stdout.write

        out(f"{event.name}: {results.score_count} scores from {results.judge_count} judges, kappa {results.kappa:g}")
        out(f"population mean {results.pop_mean:.2f}, spread {results.pop_std:.2f}; "
            f"{results.excluded_duplicates} score(s) on flagged duplicates left out\n")
        out(f"{'Rank':>4} {'Project':18} {'Reviews':>8} {'Raw':>6} {'RawRank':>8} {'Norm':>6} {'z':>6} {'Moved':>6}")
        for row in results.rows:
            if not row.reviews:
                continue
            moved = f"{row.movement:+d}" if row.movement else "0"
            reviews = f"{row.reviews}" + (f" ({row.informative})" if row.informative != row.reviews else "")
            out(f"{row.rank:>4} {row.project.title[:18]:18} {reviews:>8} {row.raw_mean:6.2f} {row.raw_rank:>8} "
                f"{row.display:6.2f} {row.z_mean:+6.2f} {moved:>6}")

        ranked = [r for r in results.rows if r.reviews]
        moves = [abs(r.movement) for r in ranked]
        out(f"\n{sum(1 for m in moves if m)} of {len(ranked)} projects changed place; "
            f"median move {statistics.median(moves):g}, largest {max(moves)}.")
        out("(Reviews in brackets: how many came from judges whose marks carry signal.)")

        judges = {str(c.user.pk): c for c in results.calibration}
        out("\nThe biggest moves, explained:")
        for row in sorted(ranked, key=lambda r: -abs(r.movement))[:options["movers"]]:
            pid = str(row.project.pk)
            out(f"\n  {row.project.title} ({row.project.external_id or row.project.pk}): raw rank {row.raw_rank}, "
                f"normalized rank {row.rank} ({row.movement:+d})")
            for jid, per_project in results.raw.items():
                if pid not in per_project:
                    continue
                c = judges[jid]
                why = c.label or (f"{c.lean} judge" if c.lean else "")
                out(f"    {c.user.external_id or c.user.display_name:8} gave {per_project[pid]:.2f}; their average "
                    f"{c.mean:.2f} over {c.count} score{'s' if c.count != 1 else ''}; z {results.z[jid][pid]:+.2f}"
                    + (f"  ({why})" if why else ""))

        flat = [c for c in results.calibration if c.label]
        if flat:
            out("\nJudges whose marks carry no signal, so they count as neutral votes:")
            for c in flat:
                out(f"  {c.user.external_id or c.user.display_name}: {c.label} ({c.count} at {c.mean:.2f})")

        if options["trials"]:
            out("")
            engine.proof(options["trials"])
