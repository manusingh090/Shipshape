"""Community voting maths. Plain Python, no Django, runnable on its own.

Two methods:

* single: one person, one vote. Each ballot backs at most one project.
* quadratic: each voter gets a budget of credits. Putting n votes on one
  project costs n * n credits, so influence grows with the square root of
  what you spend: 1 vote costs 1, 2 cost 4, 3 cost 9. Backing five projects
  with one vote each costs 5 credits; backing one project with five votes
  costs 25.

The point of quadratic voting is that intensity is expensive. A voter can
still say "this one matters a lot to me", but a group that pours everything
into one project buys much less influence than the same people spreading
support across the projects they actually rate.

On top of that there is a ceiling on the votes one ballot can give one
project (3 by default, which costs 9 of the 25 credits). The simulation
below is why: without it, a bloc of friends who all go all-in on one project
counts for about twice as much per head as a sincere fan does. With the
ceiling set near what a sincere fan gives a favourite, each member of a bloc
is worth one keen fan and no more, and sincere voters keep three levels of
enthusiasm to express.

Ballots list the projects in a different random order for every voter
(`shuffled`), because people read a list from the top and drift off: in one
fixed order, A to Z or one shuffle shared by everyone, the projects near the
top collect votes for being there. The second simulation below measures it.

Run it to reproduce the evidence in JUDGING.md, section 10:

    python src/voting/method.py --trials 1000
    python src/voting/method.py --trials 1000 --seen 20
    python src/voting/method.py --order --trials 1000
"""

import argparse
import math
import random
from dataclasses import dataclass

SINGLE = "single"
QUADRATIC = "quadratic"


class BallotInvalid(ValueError):
    """The ballot breaks the method's rules. The message is for the voter."""


def cost(votes):
    return votes * votes


def max_votes_per_project(credits, cap=None):
    """The most votes one project can take from one ballot: whatever the
    budget can pay for, and never more than the organizer's ceiling."""
    affordable = math.isqrt(credits)
    return affordable if cap is None else max(1, min(cap, affordable))


def check(method, allocation, credits, cap=None):
    """Validate {project: votes} and return the credits it spends.

    Zero means "no vote" and is dropped by the caller. Everything else has to
    be a whole number of at least 1.
    """
    for votes in allocation.values():
        if not isinstance(votes, int) or isinstance(votes, bool) or votes < 1:
            raise BallotInvalid("Votes are whole numbers, 1 or more.")
    if method == SINGLE:
        if len(allocation) > 1 or any(v != 1 for v in allocation.values()):
            raise BallotInvalid("This vote is one person, one vote: pick one project.")
        return len(allocation)
    if method != QUADRATIC:
        raise ValueError(f"unknown method {method!r}")
    ceiling = max_votes_per_project(credits, cap)
    if any(v > ceiling for v in allocation.values()):
        raise BallotInvalid(f"{ceiling} votes is the most one project can take from one ballot.")
    spent = sum(cost(v) for v in allocation.values())
    if spent > credits:
        raise BallotInvalid(f"That ballot costs {spent} credits and you have {credits}.")
    return spent


def shuffled(items, seed):
    """items in an order fixed by seed (bytes or str): the same seed always
    gives the same order, different seeds give independent orders. The caller
    passes items in a stable base order, so the result only depends on seed."""
    out = list(items)
    random.Random(seed).shuffle(out)
    return out


@dataclass
class Line:
    project: object
    votes: int = 0
    supporters: int = 0
    credits: int = 0
    rank: int = 0


def tally(ballots, projects):
    """ballots: iterable of {project: votes}. Returns Lines in rank order.

    Ranked by votes, then by supporters (breadth of support breaks a tie),
    with competition ranking (1, 2, 2, 4) for anything still level. Projects
    nobody voted for are included, with zeros.
    """
    lines = {p: Line(project=p) for p in projects}
    for ballot in ballots:
        for project, votes in ballot.items():
            line = lines.get(project)
            if line is None or votes <= 0:
                continue
            line.votes += votes
            line.supporters += 1
            line.credits += cost(votes)
    ordered = sorted(lines.values(), key=lambda l: (-l.votes, -l.supporters))
    previous, rank = None, 0
    for position, line in enumerate(ordered, start=1):
        key = (line.votes, line.supporters)
        if key != previous:
            rank, previous = position, key
        line.rank = rank
    return ordered


# ------------------------------------------------------------- simulation
# How does each method hold up against a loud minority? Simulated events
# with known project quality, so the right answer is known.

def honest_quadratic(utility, credits, cap=None):
    """Spend credits the way a sincere voter would: each extra vote goes to
    the project where it buys the most liking per credit. The next vote on a
    project already holding v votes costs 2v + 1."""
    ceiling = max_votes_per_project(credits, cap)
    votes = {p: 0 for p, u in utility.items() if u > 0}
    left = credits
    while votes:
        best, best_value = None, 0.0
        for p, v in votes.items():
            step = 2 * v + 1
            if v < ceiling and step <= left and utility[p] / step > best_value:
                best, best_value = p, utility[p] / step
        if best is None:
            break
        left -= 2 * votes[best] + 1
        votes[best] += 1
    return {p: v for p, v in votes.items() if v}


def honest_single(utility):
    liked = {p: u for p, u in utility.items() if u > 0}
    if not liked:
        return {}
    return {max(liked, key=liked.get): 1}


def honest_approval(utility):
    return {p: 1 for p, u in utility.items() if u > 0}


def spearman(xs, ys):
    def ranks(values):
        order = sorted(range(len(values)), key=lambda i: values[i])
        out = [0.0] * len(values)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
                j += 1
            for k in range(i, j + 1):
                out[order[k]] = (i + j) / 2
            i = j + 1
        return out

    rx, ry = ranks(xs), ranks(ys)
    mx, my = sum(rx) / len(rx), sum(ry) / len(ry)
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    vx = math.sqrt(sum((a - mx) ** 2 for a in rx))
    vy = math.sqrt(sum((b - my) ** 2 for b in ry))
    return cov / (vx * vy) if vx and vy else 0.0


# (label, how a sincere voter fills the ballot in, votes a bloc member puts on the target)
METHODS = [
    ("one person, one vote",
     lambda u, credits, cap: honest_single(u), lambda credits, cap: 1),
    ("approval",
     lambda u, credits, cap: honest_approval(u), lambda credits, cap: 1),
    ("quadratic, no ceiling",
     lambda u, credits, cap: honest_quadratic(u, credits), lambda credits, cap: max_votes_per_project(credits)),
    ("quadratic, ceiling",
     lambda u, credits, cap: honest_quadratic(u, credits, cap),
     lambda credits, cap: max_votes_per_project(credits, cap)),
]


def one_event(rng, projects=40, voters=300, seen=10, bloc_share=0.10, identities=1, credits=25, cap=3,
              noise=0.6):
    """One simulated vote. Returns {method label: measurements}.

    Every project has a true quality. Sincere voters look at `seen` random
    projects (nobody reads all forty), perceive each one's quality with some
    noise, and like the ones that look better than an average project. A bloc
    of friends backs one project from the bottom quarter with everything the
    method lets them give it. `identities` > 1 means each bloc member voted
    that many times, which is what an open link with no identity check allows.
    """
    quality = [rng.gauss(0, 1) for _ in range(projects)]
    weakest = sorted(range(projects), key=lambda p: quality[p])[: projects // 4]
    target = rng.choice(weakest)
    best = max(range(projects), key=lambda p: quality[p])
    bloc = round(voters * bloc_share)

    ballots = {label: [] for label, _, _ in METHODS}
    for _ in range(voters - bloc):
        looked = rng.sample(range(projects), seen)
        utility = {p: quality[p] + rng.gauss(0, noise) for p in looked}
        for label, sincere, _ in METHODS:
            ballots[label].append(sincere(utility, credits, cap))
    for label, _, brigade in METHODS:
        ballots[label].extend([{target: brigade(credits, cap)}] * (bloc * identities))

    out = {}
    for label, cast in ballots.items():
        lines = tally(cast, range(projects))
        rank_of = {line.project: line.rank for line in lines}
        votes = [0] * projects
        for line in lines:
            votes[line.project] = line.votes
        out[label] = {
            "bloc_wins": rank_of[target] == 1,
            "bloc_top3": rank_of[target] <= 3,
            "best_wins": rank_of[best] == 1,
            "best_top3": rank_of[best] <= 3,
            "rho": spearman(votes, quality),
        }
    return out


SCENARIOS = [
    ("No bloc", dict(bloc_share=0.0)),
    ("Bloc of 5% (15 friends)", dict(bloc_share=0.05)),
    ("Bloc of 10% (30 friends)", dict(bloc_share=0.10)),
    ("Bloc of 20% (60 friends)", dict(bloc_share=0.20)),
    ("Bloc of 10%, 3 identities each", dict(bloc_share=0.10, identities=3)),
]


def summarise(trials=1000, seed=7, **overrides):
    rng = random.Random(seed)
    rows = []
    for label, params in SCENARIOS:
        totals = {name: {} for name, _, _ in METHODS}
        for _ in range(trials):
            for name, measured in one_event(rng, **{**params, **overrides}).items():
                for key, value in measured.items():
                    totals[name][key] = totals[name].get(key, 0) + value
        rows.append((label, {name: {k: v / trials for k, v in t.items()} for name, t in totals.items()}))
    return rows


# ------------------------------------------------ position bias on the ballot
# Voters read the ballot from the top and give up at some point: the chance
# of even looking at the project in position i (0 = top) is exp(-i / reach).
# With reach 10 a voter looks at about 10 of 40 projects, nearly always the
# first few, rarely the last. Titles are unrelated to quality, so "A to Z"
# and "one shuffle shared by everyone" behave the same: one fixed order.

def position_event(rng, per_voter, projects=40, voters=300, reach=10.0, credits=25, cap=3, noise=0.6):
    quality = [rng.gauss(0, 1) for _ in range(projects)]
    listing = list(range(projects))
    rng.shuffle(listing)                       # the one shared order (think A to Z)
    place = {p: i for i, p in enumerate(listing)}
    ballots = []
    for _ in range(voters):
        order = rng.sample(listing, projects) if per_voter else listing
        looked = [p for i, p in enumerate(order) if rng.random() < math.exp(-i / reach)]
        utility = {p: quality[p] + rng.gauss(0, noise) for p in looked}
        ballots.append(honest_quadratic(utility, credits, cap))
    lines = tally(ballots, range(projects))
    votes = [0] * projects
    for line in lines:
        votes[line.project] = line.votes
    podium = [line.project for line in lines if line.rank <= 3 and line.votes]
    best = max(range(projects), key=lambda p: quality[p])
    return {
        "rho": spearman(votes, quality),
        "position_rho": spearman(votes, [-place[p] for p in range(projects)]),
        "podium_from_top_quarter": sum(place[p] < projects // 4 for p in podium) / max(1, len(podium)),
        "best_wins": next(line.rank for line in lines if line.project == best) == 1,
    }


def summarise_order(trials=1000, seed=11, reaches=(10.0, 20.0)):
    rng = random.Random(seed)
    rows = []
    for reach in reaches:
        for per_voter in (False, True):
            totals = {}
            for _ in range(trials):
                for key, value in position_event(rng, per_voter, reach=reach).items():
                    totals[key] = totals.get(key, 0) + value
            rows.append((reach, per_voter, {k: v / trials for k, v in totals.items()}))
    return rows


def main_order(trials, seed):
    print(f"{trials} simulated events per row: 40 projects, 300 voters, quadratic with a ceiling of 3. "
          "Voters read the ballot from the top; the chance of looking at position i is exp(-i / reach).")
    print()
    header = (f"{'projects looked at':20}{'order':30}{'votes follow position':>23}{'top 3 from first 10':>21}"
              f"{'best wins':>11}{'rho':>7}")
    print(header)
    print("-" * len(header))
    for reach, per_voter, r in summarise_order(trials, seed):
        label = "about 10" if reach == 10 else "about 17"
        order = "shuffled for each voter" if per_voter else "one order for everyone"
        print(f"{label:20}{order:30}{r['position_rho']:>22.3f} {r['podium_from_top_quarter']:>20.0%}"
              f"{r['best_wins']:>10.0%} {r['rho']:>6.3f}")


def main():
    parser = argparse.ArgumentParser(description="Community voting: a loud minority against four methods.")
    parser.add_argument("--trials", type=int, default=300)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--credits", type=int, default=25)
    parser.add_argument("--cap", type=int, default=3, help="most votes one ballot can give one project")
    parser.add_argument("--seen", type=int, default=10, help="projects each sincere voter looks at")
    parser.add_argument("--order", action="store_true", help="the ballot-order simulation instead")
    args = parser.parse_args()
    if args.order:
        return main_order(args.trials, args.seed if args.seed != 7 else 11)

    print(f"{args.trials} simulated events per scenario: 40 projects, 300 voters, each sincere voter looks at "
          f"{args.seen} of them. Quadratic budget {args.credits} credits, ceiling {args.cap} votes a project. "
          "The bloc backs one project from the bottom quarter.\n")
    header = (f"{'scenario':32}{'method':24}{'bloc wins':>10}{'bloc top 3':>11}"
              f"{'best wins':>10}{'best top 3':>11}{'rho':>7}")
    print(header)
    print("-" * len(header))
    for label, by_method in summarise(args.trials, args.seed, credits=args.credits, cap=args.cap, seen=args.seen):
        for i, (name, _, _) in enumerate(METHODS):
            r = by_method[name]
            print(f"{label if i == 0 else '':32}{name:24}{r['bloc_wins']:>9.0%} {r['bloc_top3']:>10.0%}"
                  f"{r['best_wins']:>9.0%} {r['best_top3']:>10.0%} {r['rho']:>6.3f}")
        print()


if __name__ == "__main__":
    main()
