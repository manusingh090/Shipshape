# Judging

This document explains, and defends, how Shipshape judges projects. It's written for organizers deciding whether to trust the ranking, for judges wondering what happens to their marks, and for reviewers checking the claims. The [README](README.md) says how to run the portal; [ARCHITECTURE.md](ARCHITECTURE.md) describes the code.

**Contents**

* [1. What T2 asks for, as engineering problems](#1-what-t2-asks-for-as-engineering-problems)
* [2. Judges, and who reviews what](#2-judges-and-who-reviews-what)
* [3. Weighted rubric scoring](#3-weighted-rubric-scoring)
* [4. Cross-judge normalization](#4-cross-judge-normalization)
* [5. Role isolation, enforced in the backend](#5-role-isolation-enforced-in-the-backend)
* [6. Live progress dashboard](#6-live-progress-dashboard)
* [7. CSV export at every stage](#7-csv-export-at-every-stage)
* [8. Audit trail and anti-abuse](#8-audit-trail-and-anti-abuse)
* [9. What this deliberately doesn't do](#9-what-this-deliberately-doesnt-do)
* [10. Community voting (T3)](#10-community-voting-t3)
* [11. Threat model: voting and submission abuse](#11-threat-model-voting-and-submission-abuse)

How Shipshape invites and assigns judges, scores projects against a weighted rubric, keeps judges out of each other's business, and corrects for judges who mark on different scales. Section 10 covers the other way projects get judged: the community vote. Section 11 is the threat model: the attacks on voting and submissions this portal stops, and the ones it doesn't. Every claim below can be checked by running something:

```bash
python src/judging/engine.py                           # the worked example
python src/judging/engine.py --trials 1000             # the Monte Carlo proof
docker compose exec portal python manage.py normalization_proof   # the fixture, explained (or python src/manage.py normalization_proof)
python src/voting/method.py --trials 1000              # community vote: a loud minority against four methods
python src/voting/method.py --trials 1000 --seen 20    # the same, with voters who look at more projects
python src/voting/method.py --order --trials 1000      # ballot order: one order for everyone against one per voter
python src/judging/engine.py --trials 1000 --collusion  # threat model: what colluding judges gain, how often it's flagged
python tests/attacks/probe.py .dogfood.toml             # threat model: attacks a running (throwaway) portal over HTTP
```

The algorithms live in [src/judging/engine.py](src/judging/engine.py): pure standard library, no Django, ported from the reference `scoring_engine.py` that came with the brief. Everything that touches the database is in `src/judging/`: `access.py` (isolation), `assigning.py`, `scoring.py`, `results.py`, `exports.py`, `staff.py`. Other code paths below are relative to `src/`.

## 1. What T2 asks for, as engineering problems

| Requirement | Problem shape | Where it lives |
| --- | --- | --- |
| Judge invitation and assignment, batch or algorithmic | Balanced bipartite assignment under track and conflict constraints | `staff.py`, `assigning.py`, `engine.assign_judges` |
| Weighted, organizer-configurable rubric | A deterministic weighted sum on one scale per event | `models.Criterion`, `engine.Rubric` |
| Role isolation enforced in the backend | Authorization at the query layer, not the template | `access.py`, used by every judge view and API |
| Live progress dashboard | An aggregation over the assignment table, re-read every 10 s | `results.compute_progress` |
| Cross-judge normalization, documented and defended | Statistical correction for judge severity and spread | `engine.normalize_scores`, section 4 |
| CSV export at every stage | One export per stage, one shape per table | `exports.py` |

## 2. Judges, and who reviews what

### 2.1 Getting judges in

An organizer adds a judge under **Judging, Judges** in two ways:

* **An existing account**, by email, with the tracks they may review.
* **An invite link.** There is no mail server on an offline laptop, so the organizer copies the link and sends it however they like, as with team invites. Links are single-use, expire (14 days by default), can be withdrawn, and can be tied to one email address so a forwarded link is useless.

A judge normally reviews only their own tracks. An organizer can make someone a **floater** who may review any track; that is a deliberate, visible switch, never a default.

Some people can never judge an event: its organizers and platform admins (they can see every score, so they can't also be a blind voter), and anyone on a team in it. The reverse holds too: a judge can't be made an organizer of that event or promoted to admin while judging.

### 2.2 Constraints

* Every project reaches `k` reviews (default 3, set per event).
* A judge is only ever given projects in their tracks, unless they are a floater.
* A judge is never given a project they have a conflict of interest with. Judges declare conflicts from their scoring page ("I mentored this team"); organizers can record them too. Own-team conflicts can't arise, because competitors can't judge.
* Nobody reviews the same project twice, including across batches.
* Load is balanced: each pick goes to whoever currently carries the fewest reviews.
* Neither submission order nor judge order can bias the result.

Why `k` = 3 by default: Devpost's guidance puts scoring 30 projects at about 5 hours, so around 10 minutes a review. The fixture event has 40 projects and 30 judges; three reviews each is 120 reviews, 4 per judge, well under an hour of work, where asking every judge to score every project in their track would be an afternoon. Three is also the fewest reviews at which normalization (section 4) has something to work with when one judge is unusual.

### 2.3 The algorithm: greedy, load-balanced, randomized tie-break

```
assign_judges(projects, judges, k, seed, needed, initial_load):
    load[j] = initial_load[j]            # reviews j already carries (0 in a first batch)
    group judges by track; floaters join every track's pool
    order = shuffle(projects, seed)      # no submission-order bias
    for project in order:
        want = needed[project]           # k minus the reviews it already has
        pool = judges eligible for project.track
               minus judges with a conflict or an existing review of it
        shuffle(pool, seed)              # no judge-order bias
        sort pool by load ascending      # stable, so ties stay random
        take the first `want`; load += 1 for each
        if fewer than `want`: record a shortfall (never silently drop it)
```

The reference version assigned exactly `k` per project from zero. Shipshape added `needed` and `initial_load` so a second batch tops coverage up instead of starting over, plus floaters. With neither argument it behaves exactly like the reference, down to the random numbers it draws (a test checks this against the reference's published output).

It's greedy, not a global optimum. With single-track judges it keeps every judge in a track within one review of each other; with judges spanning tracks the tests allow two. Cost is O(P · J log J), trivial at hackathon scale. If an event ever needed a provable optimum under tight capacity limits, the problem is min-cost max-flow (source to judge with capacity max_load, judge to project with capacity 1 for eligible pairs, project to sink with capacity k). It isn't needed here, and the greedy version is far easier to audit.

### 2.4 Batch and algorithmic mode are one code path

**Algorithmic**: the organizer runs a batch over everything, "every project below the target, every judge". **Batch**: the same function over a narrower slice, one track, a hand-picked set of projects, a hand-picked set of judges, which is how a second wave of judges is brought in mid-event. There is no second implementation.

Every run is **previewed first** (nothing saved: the proposal, the shortfalls and each judge's load before and after), then saved with the same seed. Saving re-plans inside the transaction against the current rows, so it reproduces the preview unless the data changed in between. The seed is stored on the batch, so any batch can be reproduced later from the same inputs. Organizers can also assign one project to one judge by hand, under the same rules, and take back an assignment that has no submitted score.

### 2.5 Defense in depth

The track and conflict rules are checked in the engine, again when assigning by hand, again inside the service that saves assignments, and once more before any score is written. When an organizer takes a track away from a judge, that judge's unfinished reviews in it are removed, so their queue can never show another track. A bug in any one layer can't turn into a judge seeing or scoring what they shouldn't.

### 2.6 What happened with the fixture

`fixtures.json` has scores but no assignments, so each of its 126 scores becomes a completed assignment in an "Imported from fixtures.json" batch. That reproduces the file faithfully, including its awkward parts: projects with two reviews next to projects with five, and the two batches that were never finished. Eight projects ended up with only two reviews, so in demo mode the seed runs, once, the batch an organizer would run on day one: top every project up to three reviews (seed 2026). It placed eight new reviews, every one inside the judge's tracks, with no shortfalls, all going to the least-loaded eligible judges. Those eight are what the progress dashboard shows as not started.

## 3. Weighted rubric scoring

The organizer defines the rubric per event under **Judging, Rubric**: criteria with a label, a short description for judges, and a weight. Weights don't need to add up to anything; they're normalized when scores are combined:

```
w'_i = w_i / Σ w_i
weighted(judge, project) = Σ w'_i · mark_i
```

Every criterion shares the event's scale (1 to 5 by default), so the weighted score stays on that scale, which section 4 depends on: normalization assumes every raw score is on one consistent scale.

The fixture event's rubric is its three criteria (functionality, quality, innovation) at equal weight, because the file says nothing about weights. An organizer can change that, and the rankings update immediately. For comparison, Devpost's online judging only supports one set of equally weighted criteria, and organizers who want weights have to judge offline ([Devpost help](https://help.devpost.com/article/64-judging-public-voting)). Here weights are part of online judging, and because nothing derived is stored, changing one mid-judging recalculates every ranking without touching a single score.

Rules that keep scores comparable:

* Once the first score is submitted, the scale is fixed and criteria can't be added or removed. Otherwise earlier scores would be missing a mark or sit on a different scale.
* Weights, labels and descriptions stay editable, because they're applied when results are computed, not stored in scores. A weight change is recorded in the activity log.
* Nothing derived is stored: weighted totals, z-scores and rankings are recomputed from the marks on every request.

The reference design allowed a rubric per track. Shipshape deliberately uses one per event: judges often cover two tracks, and a judge whose scores come from two different rubrics can't be normalized coherently against their own mean.

## 4. Cross-judge normalization

### 4.1 The problem

Same rubric, very different judges: one is harsh (everything a 2), one is generous (everything a 4.5), one gives every project the same mark. Each project only sees three of them. A plain average lets the luck of the draw decide a project's fate more than its quality does. "We averaged the scores and hoped" is the answer the brief calls weak.

### 4.2 The method: shrinkage z-scores

For each judge `j`, compare every mark with that judge's own average and spread:

```
z_ij = (raw_ij - μ_j) / σ_j
```

A harsh judge's 3 and a generous judge's 4.5 then carry the same signal when they mean the same thing relative to each judge's habits, so severity cancels out.

But `σ_j` is only trustworthy with enough scores, and plain z-scores misbehave in exactly the cases the fixture contains. With two scores, a plain z-score is always ±0.71 however close the marks were, so a 3.0 against a 3.1 becomes a full-strength verdict. A judge whose marks never vary has σ = 0 and can't be divided by at all. So each judge's variance is shrunk toward the population's, weighted by how much data the judge has:

```
σ_j² = ((n_j - 1)·σ_j,raw² + κ·σ_pop²) / (n_j - 1 + κ)
```

`κ` is the prior strength: how many projects' worth of population-typical spread every judge starts with before their own data takes over. A judge with 30 scores is judged almost entirely on their own spread; one with 2 is mostly given the population's.

Two consequences matter:

* **The flat judge.** If every mark a judge gives is the same, σ_j,raw is 0, the shrunk σ_j is still positive (no divide by zero), and since every mark equals their mean, every z they produce is exactly **0**. A ballot with no information becomes a neutral vote, rather than crashing the pipeline or quietly dragging projects toward the middle at full weight.
* **The single-score judge.** Someone with one score has nothing to compare it with, so it can't be told apart from severity, and it is also neutral (z = 0).

For display, a z-score is mapped back onto the familiar scale: `display = μ_pop + z · σ_pop`.

### 4.3 Why κ = 5: tested, not asserted

The reference picked κ = 5 without evidence, so it was tested. `python src/judging/engine.py --trials 1000` simulates events where every project has a hidden true quality, judges score it through their own bias and noise, and each project gets three reviews from the engine. It then measures how well each method's ranking matches the truth (Spearman rank correlation; 1.0 is perfect):

| Scenario (1,000 events each) | Raw average | Plain z-score | Shrinkage κ=5 | Shrinkage κ=25 | κ=5 beats raw |
| --- | --- | --- | --- | --- | --- |
| Reference judges, 12 projects | 0.766 | 0.838 | 0.841 | 0.841 | 75% of events |
| Reference judges, 40 projects | 0.809 | 0.891 | 0.890 | 0.889 | 98% of events |
| Fixture-like: 30 judges, 40 projects | 0.802 | 0.812 | 0.830 | 0.832 | 69% of events |

What that says, plainly:

* Normalizing beats raw averages on average in every scenario.
* When each judge has plenty of scores (the reference's six judges), almost all of the gain comes from centering each judge on their own mean; shrinkage barely matters.
* When judges have only a handful of scores each (the fixture's shape: 30 judges, about 4 scores each), plain z-scores barely help (+0.009), and shrinkage triples the gain (+0.028). That's the case shrinkage exists for.
* κ = 5 captures almost all of that; κ = 25 adds 0.002. Much larger κ drifts toward "centre only", which ignores genuine differences in spread. 5 is the defensible middle, and organizers can change it per event.
* It is not magic. In the fixture-like regime normalization beats the raw average in 69% of events, not all of them, because with four scores per judge every judge's calibration is itself noisy. Section 4.7 shows how the portal makes that visible.

### 4.4 From z-scores to a ranking

```
final(project) = mean(z_ij over every judge j who scored it)
```

Equal weight per judge, deliberately. Weighting judges by "reliability" (agreement with consensus) was considered and rejected as a default: it's harder to explain, and it creates a feedback loop where early scores decide how much later ones count. Equal-weight, per-judge-corrected z-scores are the version that can be explained in one paragraph to a team that just placed fourth because of it.

Equal scores share a rank (1, 2, 2, 4), for the raw ranking and the normalized one alike, and values are compared at nine decimal places, so floating-point noise from adding the same marks in a different order can never turn a tie into a win. Scores are also always read in the same order. (An earlier version broke ties by summation order, and two independently seeded databases disagreed about which of two 3.50 projects was ahead; there's a test for this now.) Only submitted scores on listed projects count: drafts never do, and the duplicate submission's scores are left out (section 4.7).

### 4.5 Proof, part one: the worked example

`python src/judging/engine.py` runs the reference's demo unchanged: a harsh, a lenient and a flat judge plus three ordinary ones, twelve projects, three reviews each.

```
Project    RawAvg  RawRank    NormZ  NormRank  Movement
P07          4.42        1     0.97         1         0
P06          4.14        2     0.80         2         0
P01          3.07        5     0.73         3        +2
P05          3.53        3     0.47         4        -1
P03          2.62        7     0.13         5        +2
P12          2.92        6    -0.13         6         0
P04          3.14        4    -0.20         7        -3
P09          2.39        8    -0.33         8         0
P11          2.13       11    -0.33         9        +2
P10          2.38        9    -0.34        10        -1
P08          2.29       10    -0.46        11        -1
P02          1.76       12    -1.30        12         0

Who each big mover drew (and its hidden true quality):
  P01 moved +2: true quality 3.60, judged by J-harsh, J-norm-c, J-flat
  P03 moved +2: true quality 2.69, judged by J-flat, J-norm-a, J-harsh
  P04 moved -3: true quality 2.56, judged by J-lenient, J-flat, J-norm-a
  P11 moved +2: true quality 2.55, judged by J-harsh, J-norm-c, J-norm-b

rank correlation with true quality: raw 0.888, normalized 0.937
```

P04 drops three places because it drew the **lenient** judge, whose inflated mark lifted its raw average (3.14) far above its true quality (2.56). P01, P03 and P11 each rise two because they drew the **harsh** judge. The normalized ranking is closer to the hidden truth (0.937 against 0.888).

### 4.6 Two corrections to the reference write-up

The reference JUDGING.md got its numbers right but explained two of them wrongly, and both would have misled a reader:

1. It said P04 fell "because it drew two harsh-leaning judges" and P01, P03 and P11 rose "because they drew the lenient judge". It's the other way round, as the coverage printed above shows.
2. It described the per-judge z spread as "~1.0 per judge by construction". With shrinkage that's false: a judge whose own spread is smaller than the population's is pulled up toward it, so their z spread falls below 1 (0.58 for the harsh judge here), and the flat judge's is exactly 0. `engine.py` now prints each judge's raw spread, shrunk spread and z spread so this can be seen.

### 4.7 Proof, part three: the fixture itself

`python src/manage.py normalization_proof` applies the method to the real fixture and explains its biggest moves from the underlying marks. On a fresh seed: 122 scores count, from 30 judges (the 4 on the flagged duplicate are left out); the population mean is 3.56 with a spread of 0.65; 33 of 40 projects change place, the median move is 2.

The biggest moves, and why:

* **Dry Harbour, 29th to 8th.** Its raw average was dragged down by jdg_01, whose only score anywhere was a 2 on every criterion. One score says nothing about whether jdg_01 is harsh or the project is weak, so it counts as neutral. Its other reviewers mostly put it well above their own averages; jdg_26, who has nine scores averaging 3.70, gave it 4.67.
* **Small Relay, 12th to 29th.** It had two reviews. One was jdg_07, who gave every project a 4: neutral once normalized. The other, from a judge with nine scores, sat slightly below that judge's average. It's also one of the unfinished-batch projects the top-up gave a third judge; that review will settle it.
* **Flat Meadow, 23rd to 37th.** A raw 3.44 looks middling, but all three of its judges marked it below their own averages, and two of them are generous markers (jdg_02 averages 4.22, jdg_30 averages 4.08). For those judges a 3.33 or 3.67 is a low mark.
* **Glass Beacon (+8) and Paper Anchor (-11)** have the same raw average, 3.50 (they share 19th place), and normalization separates them. Glass Beacon's higher mark, 3.67, came from jdg_19, who averages 3.22, so it was well above what that judge usually gives. Paper Anchor's lower mark, 3.33, came from jdg_26, who averages 3.70, so it was well below theirs. jdg_18 reviewed both as well, but has only two scores, so shrinkage keeps those verdicts small (z of 0.28 either way).

The output is deterministic: the same fixture seeded into two different databases (a Linux container and a Windows machine) produces the proof line for line.

Three judges carry no signal and count as neutral votes: jdg_07 (three scores, all 4.00), jdg_01 and jdg_23 (one score each).

That also shows the method's real limit on this data. A project whose reviews mostly come from judges with no signal is ranked on very little. The results page counts **informative reviews** (from judges with two or more scores that vary) next to the plain count, and flags any project resting on fewer than two, so an organizer knows which rankings a third review would move.

### 4.8 Limits, stated plainly

* Z-scoring assumes each judge's batch is a fair sample of the field. Random, load-balanced assignment is what makes that reasonable; a judge handed only the best projects would look harsh and be over-corrected.
* Small batches make every judge's calibration noisy. Shrinkage limits the damage; the informative-review flag makes it visible; more reviews per project fix it.
* A single-score or flat judge contributes nothing. That's correct for a ballot with no information, but it means their reviews don't count toward a project's evidence.
* One scale per event is required.

## 5. Role isolation, enforced in the backend

Every score is a row `(judge, project, marks, comment, submitted_at)` that can only exist for an assignment (a one-to-one link), and the assignment table is what grants a judge access.

* **Judge-facing queries always start from the session.** `access.my_assignments`, `my_assignment` and `my_scores` filter on the signed-in user. A judge id or project id from the client is never used to decide what to show.
* **The assignment table gates existence.** A judge who asks for a project they weren't assigned, in any track, gets the same **404** as for a project that doesn't exist, so guessing ids reveals nothing, including which track a project is in.
* **Another judge's scores are refused, not filtered.** `GET /api/judge/scores?judge=<someone else>` answers **403** to a judge, before any lookup, so the answer is the same whether that judge exists or not. Only organizers of the event (and admins) may read another judge's scores.
* **Track isolation uses the same gate.** Assignments are only ever created inside a judge's tracks, taking a track away removes unfinished reviews in it, and the track is checked again before any score is written.
* **A judge never sees other judges' marks or comments**: not on the scoring page, not in their queue, not in the API. Rankings, progress, calibration and exports are organizer-only until an organizer publishes the results.
* **Publishing is a decision, and it's final.** An organizer publishes the ranking from Judging, Results once submissions have closed; that closes judging, so nothing on the published page can move. Everyone who can see the event gets the results page: winners, the ranking (rank, project, team, track, normalized score, number of reviews) and the community vote once it's out. A team also sees its own place, and if the organizer chose to share feedback, its average mark per criterion and the judges' comments, shuffled and without names. Raw marks, z-scores, calibration and who judged what stay organizer-only.
* **Nobody who can see every score can also judge** (organizers, admins), and nobody competing can judge their own event.

The reference suggested Postgres row-level security as a last line. Shipshape runs on SQLite, which has none, so `judging/access.py` is that layer, and it's tested the way an attacker would probe it: raw requests with another judge's id, another track's project id, a participant's cookie, no cookie at all (`tests/test_judging.py`). The acceptance checker's own probes pass:

```
T2  judge sees own scores ............. PASS
T2  judge cannot see peer scores ...... PASS
T2  participant blocked ............... PASS
T2  csv export works .................. PASS
```

## 6. Live progress dashboard

**Judging, Progress** answers "who hasn't started?" at the top: every judge who owes reviews and hasn't saved a score since that work was handed to them, with how many they owe, since when, and their email. Below that are the headline numbers (reviews submitted, judges not started, projects fully reviewed, projects short of judges), a table of every judge with their queue, each batch with who hasn't started it, the projects still waiting, and coverage by track.

"Not started" is about outstanding work, not history: a fixture judge who finished their imported scores but hasn't touched a new top-up review is not started on it.

The page re-reads a server-rendered fragment every 10 seconds while the tab is visible, which is plenty at hackathon scale; a websocket layer would solve a problem this event doesn't have. The same data is available as JSON at `GET /api/events/<slug>/judging/progress` (organizers only).

## 7. CSV export at every stage

In the organizer console's own **CSV exports** tab (the files cover every stage, not just judging), or `GET /api/events/<slug>/export/<stage>.csv` with an organizer's session:

| Stage | File | One row per |
| --- | --- | --- |
| Registration | `teams.csv` | team member |
| Submission | `projects.csv` | project, with status, duplicate flag and review coverage |
| Staffing | `judges.csv` | judge, with tracks and progress |
| Assignment | `assignments.csv` | assignment, with its batch and status |
| Scoring | `raw-scores.csv` | submitted score: one column per criterion, the weighted total, whether it counts |
| Normalization | `normalized-scores.csv` | judge and project: raw, z and display score (the reference's export shape) |
| Results | `final-rankings.csv` | project: rank, raw rank, normalized score, movement, informative reviews |
| History | `score-history.csv` | every save of every score |
| Community vote | `community-votes.csv` | project: rank, votes, backers, credits spent, share |
| Community vote | `ballots.csv` | line of a ballot: kind, voter, when, project, votes, a short network hash, whether it's counted, why it was left out, and its warning signs |
| Community | `comments.csv` | comment, including removed ones, who removed them and why |
| Audit | `audit-log.csv` | activity log entry, including refused late edits |

Text from users is defused against spreadsheet formula injection: a cell starting with `=`, `+`, `-` or `@` gets a leading apostrophe, so a project called `=HYPERLINK(...)` is shown, not run, when opened in Excel. Numbers, including negative z-scores, are left as numbers.

## 8. Audit trail and anti-abuse

This section covers the whole portal, not just judging. The code is in `src/integrity/`: `limits.py` (every rate limit, in one table), `detect.py` (duplicate and abuse detection), `services.py` (what an organizer can do about it) and `views.py` (the **Integrity** tab in the organizer console).

The design rule: **the portal refuses what is clearly a script, and flags what might be cheating for a person to judge.** A hackathon venue puts hundreds of honest people behind one network address, on the same browser, voting for the popular project, which is exactly what ballot stuffing looks like. Blocking on those signals would punish the crowd. So hard limits are set well above what a crowd does, and the patterns in between are shown to an organizer with the reason, who decides, and whose decision is logged.

### 8.1 Rate limits

| What's limited | Limit | Against |
| --- | --- | --- |
| Wrong passwords for one address | 5 per 15 minutes | Guessing one account's password |
| Wrong passwords from one network | 30 per 15 minutes | Guessing across many accounts |
| New accounts from one network | 20 per hour | Scripted sign-ups (fake voters in a signed-in vote) |
| Project saves by one person | 30 per minute | Hammering the submission form or API |
| Score saves by one judge | 30 per minute | A scripted or runaway judging session |
| Voting links sent to one address | 3 per 15 minutes | Mail-bombing someone through the email-gated vote |
| Voting links from one network | 20 per hour | Collecting addresses to vote with |
| New ballots from one network | 100 per hour | Ballot stuffing by script (smaller bursts are flagged, 8.2) |
| Ballot saves by one voter | 20 per minute | Hammering the ballot |
| Comments by one person | 5 per minute, 30 per hour | Floods and spam |

Refusals answer `429` on the API and a plain message on the page. Each is checked before the write's transaction opens, so the refusal's audit line isn't rolled back with it. **One line per burst:** the first refusal for a key in a window goes into the audit log, and the rest of that window's refusals don't, so a script hammering an endpoint leaves one readable line rather than burying everything else. Limits and ballots never store a network address, only a SHA-256 of it salted with the site's secret. (The account page's list of your signed-in sessions does keep each session's address, so you can recognise your own devices.) The same table is shown on the Integrity page, so organizers know what the portal already stops.

### 8.2 Duplicate detection

Ballots get a warning sign for each of these, with the reason in words:

| Sign | Meaning |
| --- | --- |
| Burst | One of 5 or more new ballots from one network within 10 minutes |
| Same device | The same network and browser as a ballot from someone else |
| Copied | Exactly the same votes as 3 or more other ballots |
| Alias | An address that reaches the same inbox as another voter's (`a.b@gmail.com`, `ab+2@googlemail.com` and `ab@gmail.com` are one inbox). Email ballots themselves now belong to the inbox (11.4), so this sign is left for an email ballot next to an account ballot, and for ballots cast before that change |
| Fresh account | The account was made less than 30 minutes before its ballot |

The Integrity page lists flagged ballots ranked by how many signs each shows, with its votes, when it came, and a short network fingerprint. One sign alone is often innocent; two or more is worth a look, and the page counts those separately. Judges: a project where one judge's normalized score is 1.5 or more from the mean of the rest of its panel, which is what boosting a friend looks like (11.3, JC3; the trade-off is measured in 11.7). Projects: two listed submissions with the same repository (normalized: scheme, `www.`, `.git` and trailing slashes don't matter) or the same title, whether from one team or two. Comments: the same text, ignoring case and spacing, on three or more projects. The fixture raises nothing: its only duplicate, `prj_41`, is already resolved.

### 8.3 What an organizer can do

Every action needs a reason, goes into the audit log with it, and can be undone.

* **Leave ballots out of the count.** Tick them, give the reason ("12 ballots from one laptop in 3 minutes"), and they stop counting in the tally, the results and the count export. The ballot stays in the database and the ballots export (with `counted`, `excluded_reason` and `flags` columns), and "count it again" restores it. The voter isn't told, so a cheat learns nothing about what got caught.
* **Hide a duplicate submission** as a duplicate of another, the same state the fixture's `prj_41` is in: out of the gallery, the ballot and the rankings. Which one is listed can be swapped later from Submissions.
* **Remove repeated comments** in one go.

This is what the publish step in section 10.6 is for: when voting closes, the voting page says how many counted ballots still show two or more signs, next to the Publish button.

### 8.4 Reading the audit trail without a database client

* **Each event's Activity tab** is its whole trail, newest first, server timestamps, never edited. It filters by kind (refused and rate-limited, anti-abuse decisions, judging, community vote, submissions and teams, settings and staff) and searches names, emails and text, with the whole log as CSV (`audit-log.csv`). Refused lines are marked, so a late edit or a rate limit stands out.
* **The admin's platform audit log** (`/admin/audit/`, with the latest entries on the admin page) holds what belongs to no single event: sign-in lockouts, sign-up limits, and admins changing someone's role or deactivating them, each with who did it.
* **What's logged:** refused late edits and roster changes, rate-limit refusals, every score submission and change (plus the append-only score history), assignments, conflicts, rubric and weight changes, judge invites, voting settings, voting links, publishing, ballot exclusions and restorations, hidden duplicates, comment removals, event settings and staff changes.
* A team's own history (team and submission pages) never shows judging or voting entries (section 5).

Judging keeps its own guarantees on top:

* **Score history is append-only.** Every save writes a `ScoreRevision` with the marks, the comment and a timestamp; corrections are new rows, never edits. Imported fixture scores have a revision too.
* **One ballot per judge per project**, enforced by unique constraints, so a second submission updates the score (and adds a revision) rather than creating a second vote. A submitted score can be changed until judging ends but never turned back into a draft.
* **The judging window holds like the submission deadline.** Scoring opens when submissions close and closes at the event's judging end date, checked with the server clock inside the write.

### 8.5 What it doesn't catch, plainly

* A patient cheat with several phones on several networks, several real inboxes or several real accounts, voting minutes apart and not identically, raises no sign. Only identity checks outside the portal stop that; email-gated or signed-in voting raise the cost, and an open link raises it least.
* Signs are heuristics. A family on one wifi voting for their cousin looks like a burst. That's why nothing is removed automatically.
* Section 11.5 is the fuller list, attack by attack.
* Rate limits are per network, and everyone behind one address shares them. The per-network limits are generous for that reason, which also means a script spread thinly under them isn't refused, only flagged.

## 9. What this deliberately doesn't do

* No reliability weighting in the final aggregate (section 4.4).
* No global-optimum assignment solver (section 2.3).
* No per-track rubrics (section 3).
* No email: judges are invited by link.

## 10. Community voting (T3)

Judges aren't the only audience. T3 asks for a community vote "with configurable access: open link, email-gated, or authenticated", or "something better than one-person-one-vote, if you can defend it", naming quadratic voting. This section is the defence, with the numbers. The code is in `src/voting/`: `method.py` (the maths and the simulation, plain Python), `services.py` (every rule), `results.py` (the count), `views.py` and `api.py`.

### 10.1 Who can vote: three access modes

The three modes are the ones Devpost for Teams shipped in 2025 ([release notes](https://info.devpost.com/blog/devpost-for-teams-releases-q1-2025)): anyone with the link, email only, or sign-in required. What changes between them is what counts as "one voter".

| Mode | One ballot per | What it costs a voter | How it's gamed |
| --- | --- | --- | --- |
| Open link | browser session that opened the organizer's secret link | nothing | clear cookies, vote again |
| Email-gated | confirmed email address | typing an address, clicking a link | one ballot per address you control |
| Signed in | Shipshape account | making an account | one ballot per account you make |

* **Open link.** The link carries a random token. The plain ballot address says "by invitation" and never shows it, and neither does the event page. An organizer can replace the link, which kills the old one at once. It's the easiest mode to hand out (a QR code on the venue screen) and the easiest to stuff, and the console says so.
* **Email-gated.** The voter types an address and gets a one-time link, valid for 30 minutes. Only a SHA-256 hash of the token is stored, like a password reset, so a copy of the database doesn't let anyone vote as someone else. Opening the link shows a button, and only pressing it uses the link up: mail scanners that pre-fetch links would otherwise burn every link before its owner clicked. The confirmed address then lives in the session. Someone who is signed in votes with their account's address and no other: the form shows it instead of asking, a link for a different address won't open for them (and isn't used up), and an address confirmed earlier in the same browser stops counting once a different account signs in. They still get the link, because accounts here don't verify their address at sign-up, so without it anyone could sign up as a made-up address and vote as it. At most three links per address per 15 minutes, and 20 per network per hour. The organizer can restrict addresses to their own domains (`uni.edu`). With no mail server configured (`EMAIL_HOST`), messages are written to `DATA_DIR/outbox` instead, because the portal has to run offline; in demo mode the page also shows the link it would have mailed.
* **Signed in.** One ballot per account, and nothing else to set up.

Whatever the mode, two rules hold, checked against every account the voter is known to be (the ballot's owner, whoever is signed in, the account behind a confirmed address): **organizers and admins can't vote**, because they can watch the count, and **nobody can vote for their own team's project**. An anonymous open-link voter is the one case where the portal can't know who they are; section 10.4 says what that costs.

### 10.2 The method: quadratic voting, with a ceiling

Every voter gets a budget of credits (25 by default). Putting *n* votes on one project costs *n²* credits:

| Votes on one project | 1 | 2 | 3 | 4 | 5 |
| --- | --- | --- | --- | --- | --- |
| Credits | 1 | 4 | 9 | 16 | 25 |

So influence grows with the square root of what you spend, which is how the brief describes it. Caring a lot is allowed but expensive: backing five projects with a vote each costs 5 credits, backing one project with five votes costs all 25. DoraHacks, who have run quadratic votes at hackathons since 2020, argue that it lets a niche project that matters a lot to some people compete, where a plain vote can turn into a popularity contest ([DoraHacks](https://dorahacks.io/blog/news/qf-retrospective/)).

On top of that, **one ballot can give one project at most 3 votes** (9 credits). The ceiling is our addition, and section 10.3 is why. The count is the sum of votes per project. Ties go to the project more people backed; anything still level shares a place (competition ranking, as in judging). Only projects listed in the gallery count: if an organizer flags one as a duplicate after people voted for it, those votes drop out of the count but stay in the ballots export.

One person, one vote is available too, and the organizer can change the credit budget (4 to 400) and the ceiling (1 to 20). A ceiling of 1 with a big budget is approval voting. Once the first ballot is in, the access mode, method, budget and ceiling are fixed, so every ballot in the count was cast under the same rules. The dates can still move.

### 10.3 The evidence: a loud minority, simulated

The worry the brief names is a loud minority deciding the outcome. `method.py` simulates it: 40 projects with a known true quality and 300 voters. Sincere voters look at a random handful of projects (nobody reads all forty), see each one's quality with some noise, and vote for the ones that look better than average. Under quadratic voting they spend credits the way a sincere voter would, each extra vote going where it buys the most liking per credit. A bloc of friends backs one project from the bottom quarter with everything the method lets them give it. We measure how often the bloc's project wins or makes the top three, how often the genuinely best project wins, and the rank correlation between the result and true quality. Each row is 1000 simulated events.

Voters who each look at **10** projects (`python src/voting/method.py --trials 1000`):

| Scenario | Method | Bloc wins | Bloc in top 3 | Best project wins | Rank correlation |
| --- | --- | --- | --- | --- | --- |
| No bloc | one person, one vote | 0% | 0% | 79% | 0.926 |
|  | approval | 0% | 0% | 17% | 0.966 |
|  | quadratic, no ceiling | 0% | 0% | 64% | 0.984 |
|  | **quadratic, ceiling 3** | 0% | 0% | 48% | 0.984 |
| Bloc of 5% (15 friends) | one person, one vote | 0% | 0% | 83% | 0.849 |
|  | approval | 0% | 0% | 19% | 0.957 |
|  | quadratic, no ceiling | 0% | 0% | 63% | 0.945 |
|  | **quadratic, ceiling 3** | 0% | 0% | 49% | 0.964 |
| Bloc of 10% (30 friends) | one person, one vote | 1% | 59% | 81% | 0.822 |
|  | approval | 0% | 0% | 18% | 0.942 |
|  | quadratic, no ceiling | 0% | 5% | 61% | 0.899 |
|  | **quadratic, ceiling 3** | 0% | 0% | 47% | 0.936 |
| Bloc of 20% (60 friends) | one person, one vote | 95% | 100% | 6% | 0.800 |
|  | approval | 15% | 54% | 15% | 0.862 |
|  | quadratic, no ceiling | 100% | 100% | 0% | 0.867 |
|  | **quadratic, ceiling 3** | 57% | 99% | 24% | 0.869 |
| Bloc of 10%, 3 identities each | one person, one vote | 100% | 100% | 0% | 0.804 |
|  | approval | 100% | 100% | 0% | 0.849 |
|  | quadratic, no ceiling | 100% | 100% | 0% | 0.867 |
|  | **quadratic, ceiling 3** | 100% | 100% | 0% | 0.866 |

Voters who each look at **20** projects (`--seen 20`; the 5% rows are left out here, and every bloc number in them is zero):

| Scenario | Method | Bloc wins | Bloc in top 3 | Best project wins | Rank correlation |
| --- | --- | --- | --- | --- | --- |
| No bloc | one person, one vote | 0% | 0% | 90% | 0.880 |
|  | approval | 0% | 0% | 26% | 0.981 |
|  | quadratic, no ceiling | 0% | 0% | 84% | 0.992 |
|  | **quadratic, ceiling 3** | 0% | 0% | 82% | 0.992 |
| Bloc of 10% (30 friends) | one person, one vote | 0% | 40% | 91% | 0.774 |
|  | approval | 0% | 0% | 23% | 0.971 |
|  | quadratic, no ceiling | 0% | 0% | 80% | 0.928 |
|  | **quadratic, ceiling 3** | 0% | 0% | 78% | 0.953 |
| Bloc of 20% (60 friends) | one person, one vote | 29% | 100% | 69% | 0.757 |
|  | approval | 0% | 0% | 23% | 0.952 |
|  | quadratic, no ceiling | 40% | 99% | 52% | 0.879 |
|  | **quadratic, ceiling 3** | 0% | 1% | 78% | 0.908 |
| Bloc of 10%, 3 identities each | one person, one vote | 65% | 100% | 36% | 0.758 |
|  | approval | 0% | 0% | 22% | 0.938 |
|  | quadratic, no ceiling | 97% | 100% | 3% | 0.876 |
|  | **quadratic, ceiling 3** | 2% | 55% | 76% | 0.891 |

### 10.4 What the numbers say

* **One person, one vote is the easiest to hijack.** A tenth of the voters voting as one puts a weak project in the top three in 59% of events; a fifth of them wins outright 95% of the time. To be fair to it, it's the best at crowning the single best project when nobody games it (79% and 90%), because each voter's one vote goes to their favourite. But it ranks everything below the winner worst (rank correlation 0.926 and 0.880 with no bloc at all), and "when nobody games it" isn't the case a public vote should be designed for.
* **Plain quadratic voting helps, but not enough.** Its bloc member puts 5 votes on the target, while a sincere voter's favourite gets 2 or 3, so per head the bloc counts about double. At a fifth of the voters, the bloc wins every time.
* **The ceiling fixes that.** Capped at 3, a bloc member counts as one keen fan and no more. A 10% bloc never reached the top three in either table. With voters looking at 20 projects, a 20% bloc never won and made the top three in 1% of events, where plain quadratic let it win 40% of the time. The cost is some ability to crown the best project when voters look at only 10 (48% against 64% uncapped); at 20 it's almost free (82% against 84%). Its rank correlation is the best or joint best in every scenario where the bloc is made of real people.
* **Approval voting is the hardest to brigade and the worst at picking a winner.** When everyone approves every project they like, the top handful tie, and which one wins comes down to who happened to look at it: 17 to 26% of the time it's the best. A people's choice award that's a lottery among the good projects isn't a result we'd defend either.
* **Nothing survives fake identities when attention is thin.** A 10% bloc voting three times each wins under every method when voters look at 10 projects. DoraHacks make the same point: quadratic voting's weak spot is cheap identities, and they check for sybil votes after each round and drop their weight. Here that's the access mode's job. An open link lets anyone with a private window vote again, which is why the console warns about it and why email-gated or signed-in voting is the setting to use when anything is at stake. Detecting and discounting fake ballots is the anti-cheat part of T3, which comes later.
* **Attention matters as much as the method.** Every method does better when voters look at more projects, and the ceiling's cost almost disappears. So the ballot lists every project on one compact page with a live credit meter. The simulation's assumption that voters look at random projects is what per-voter ballot order delivers (section 10.5): with a fixed order, everyone's attention lands on the same first few projects.

The defaults (quadratic, 25 credits, ceiling 3) come from these tables. The model is simple on purpose: real voters are neither this sincere nor this random, and a real bloc might not go all-in. What it shows is the mechanism, and that holds up: under any method, a bloc's power is its per-head influence on one project compared with a sincere voter's, and the ceiling is the knob that sets that ratio to about one.

### 10.5 Ballot order: shuffled for each voter

People read a ballot from the top and drift off before the end, so wherever a project sits on the list changes how many people even look at it. Devpost for Teams lets organizers pick alphabetical A to Z or Z to A, a custom order, or randomized ([release notes](https://info.devpost.com/blog/devpost-for-teams-releases-q1-2025)). The detail that matters is what "random" means. One shuffle shared by every voter is no better than A to Z: it just decides at random which project gets the good spot. Only a different order for every voter spreads attention evenly.

So each ballot here is shuffled for its voter, and the order is stable: the same every time they come back, before and after saving, so their marks never jump around. The seed comes from who the voter is, mixed with the site's secret so nobody can work out another voter's order: the account for signed-in voting, the confirmed address for email voting (so the same order on any device), and a random token kept in the browser session for an open link. The order is computed on each request and never stored. Organizers can still choose A to Z, and the settings say what it costs. Like the other rules, the choice is fixed once the first ballot is in.

The evidence, from `python src/voting/method.py --order --trials 1000`: 40 projects, 300 voters, quadratic with a ceiling of 3. A voter looks at the project in position *i* with probability exp(-*i* / reach), so nearly always the first few and rarely the last. "Votes follow position" is the rank correlation between a project's votes and how high it sits in the shared order; 0 means position doesn't matter. With no bias, a quarter of the top three would come from the first ten projects.

| Projects looked at | Order | Votes follow position | Top 3 from the first 10 listed | Best project wins | Rank correlation with quality |
| --- | --- | --- | --- | --- | --- |
| about 10 | one order for everyone | 0.572 | 94% | 13% | 0.735 |
| about 10 | **shuffled for each voter** | -0.005 | 25% | 51% | 0.984 |
| about 17 | one order for everyone | 0.331 | 74% | 27% | 0.907 |
| about 17 | **shuffled for each voter** | 0.002 | 26% | 76% | 0.991 |

With one order for everyone, the top three came almost entirely from the first quarter of the list, and the genuinely best project won 13% of the time. Shuffled for each voter, position stops mattering (a correlation of zero, and the fair 25% share), and the best project wins about four times as often. This is also the assumption the loud-minority simulation in 10.3 rests on: voters there look at random projects, which is what per-voter shuffling produces.

### 10.6 Rules the server enforces

* **One ballot per voter**, by unique constraints on (event, account) and (event, inbox), and by the session for open links. Saving again replaces the ballot's lines.
* **The budget, the ceiling and whole numbers** are checked by `method.check` inside the save, whatever the browser's meter says. A ballot can't name a project that isn't on it (another event's, a draft, a flagged duplicate).
* **The window holds like the deadline.** Voting opens when submissions close (or later), so everyone votes on final projects, and closes at the organizer's closing time. Both are checked with the server clock inside the transaction, and the closing instant counts as closed. An organizer can't turn a vote on without a closing time.
* **Organizers and admins can't vote, and nobody backs their own team** (section 10.1).
* **Results are hidden from everyone but organizers while voting is open**, and after. Devpost's own advice is to avoid "showing the results live until you review your votes", and on Devpost only an event's managers can see results during the voting period ([Devpost help](https://help.devpost.com/article/64-judging-public-voting), [community voting](https://help.devpost.com/hc/en-us/articles/4406643998996-Community-and-public-voting-for-prizes)). So nobody else sees a number, not even turnout: not on the results page, not through `/api/events/<slug>/vote/results` (`403 results_hidden`, saying only whether voting is open or under review), not in the exports. A live count is also what makes piling on and collusion possible, the DoraHacks point in 10.4. When voting closes the results still wait: an organizer looks over the ballots (the ballots export shows when and from which network each came) and presses Publish. Reopening voting takes published results down. The count is computed from the ballots on every request and never stored. Devpost also suggests keeping a community prize small, so there's less reason to cheat; that's the organizer's call.
* Each ballot keeps a hash of the network it was created from (salted with the site's secret, never the raw address) and the browser's user agent. The detectors in section 8.2 use them to flag bursts and shared devices, and an organizer can leave flagged ballots out of the count after review (section 8.3).

### 10.7 Answering people who try to cheat it

The whole of section 8 applies: rate limits on ballots, links and sign-ups, warning signs on suspicious ballots, and an organizer who can leave ballots out with a logged reason before publishing. What it can't do is also said there (8.5): the method (10.3) and the identity each access mode requires carry most of the weight.

## 11. Threat model: voting and submission abuse

This is the bonus challenge: sybil votes, ballot stuffing, submission scraping, judge collusion and deadline gaming, with the attacks this portal stops and the ones it doesn't. It was written by reading the code for each attack, then trying the attack. Writing it turned up four holes and one small leak, which are fixed (11.4). What's left open is in 11.5, in the same detail as what's closed.

Every claim here can be checked:

```bash
python src/manage.py test tests.test_threat_model   # one test per attack id below, including tests that pin the gaps
python tests/attacks/probe.py .dogfood.toml         # attacks a running portal over HTTP (use a throwaway container: it changes state)
python src/judging/engine.py --trials 1000 --collusion   # what colluding judges gain, and how often it's flagged
python src/voting/method.py --trials 1000           # what a bloc of real voters gains under each voting method (10.3)
```

The words used for each outcome:

* **Stopped**: the server refuses, whichever way the request arrives (page, old API, REST API).
* **Capped**: the attack works, but the portal limits how much it can change the result.
* **Flagged**: the attack works, and it shows up on the organizer's Integrity page with the reason. Nothing is removed automatically (8.3 says why).
* **Open**: the attack works and nothing inside the portal notices. These are the honest list.

### 11.1 What's worth attacking

1. **The judges' ranking**, and the prizes that come from it.
2. **The community count**, which decides the community prize.
3. **The deadline**: everyone gets the same 72 hours, and a project is judged as it was when time ran out.
4. **Teams' work before it's public**: an idea, a description, a repository link.
5. **People's data**: participants' email addresses, judges' individual scores, private answers on the submission form.

### 11.2 Who attacks, and what they can do

| Who | Wants | Can |
| --- | --- | --- |
| A stranger with a script | To scrape, to stuff a vote | Send any HTTP request, from as many networks as they can rent; no account needed |
| A participant | Their project to win | Everything above, plus an account, a team, and as many email addresses and accounts as they care to make |
| A bloc | One project to win the community vote | Real people, each with a real inbox and account, voting together |
| A judge | To help a friend or sink a rival | Score the projects they're given, declare or hide conflicts, vote in the community vote |
| An organizer | Anything | Nearly everything: dates, weights, judges, ballots, awards. They're trusted; the defence is that every such action is logged with who did it, not that it's impossible |
| The host (admin, whoever runs the container) | | Out of scope: they have the database and the signing key |

What the model assumes:

* **The server's clock is right.** Every window (submissions, judging, voting) is judged by it; nothing a client sends about time is used.
* **`REMOTE_ADDR` is the visitor.** `X-Forwarded-For` is ignored on purpose (`accounts/throttle.client_ip`), because anyone can write it. That holds for the shipped setup, where gunicorn faces the network directly. Behind a reverse proxy every visitor would share the proxy's address, and the per-network limits would then apply to the whole crowd at once. This portal doesn't support trusted proxies; running behind one needs that added first.
* **An inbox is a person** in email-gated votes, and **an account is a person** in signed-in votes. Both are cheap to multiply, which is most of 11.5.

### 11.3 The attacks

**Sybil votes** (one person, many voters):

| Id | Attack | Result | How | Evidence |
| --- | --- | --- | --- | --- |
| SY1 | Vote twice with one account, or one confirmed address | Stopped | Unique constraints on (event, account) and (event, inbox); saving again replaces the ballot | `test_a_member_votes_and_can_change_their_mind` |
| SY2 | Vote once per spelling of one inbox: `me+1@`, `me+2@`, `m.e@gmail.com` | Stopped (fixed, 11.4) | An email ballot belongs to the inbox (`voting/services.inbox`): `+tags` dropped everywhere, dots too for Gmail. The per-address link limit counts the inbox as well, so `mallory+0..3@` gets 3 links, not 4 | `test_SY2_*`; probe `SY2` |
| SY3 | Vote once per real inbox the attacker controls | Open, slowed | 20 links per network per hour; bursts from one network are flagged; an organizer can restrict the vote to their own domains (`email_domains`), which makes this cost real accounts at that school or company | `test_SY3_gap_*`, `test_domains_can_be_restricted` |
| SY4 | Vote once per throwaway account in a signed-in vote (account addresses are never verified) | Open, slowed, flagged | 20 new accounts per network per hour; a ballot from an account made less than 30 minutes before is flagged | `test_SY4_gap_*` |
| SY5 | Open-link vote: clear cookies or open a private window for a new ballot | Open, capped, flagged | 100 new ballots per network per hour; bursts, same-device and copied ballots are flagged. The open link is the weakest mode and the console says so | `test_SY5_gap_*` |
| SY6 | Forge `X-Forwarded-For` to look like many networks | Stopped | The header is never read | `test_SY6_*`; probe `SY6`: refused after 20 links, whatever the header said |
| SY7 | Back your own team from an alias, a second address, or an open link while signed in | Stopped | Eligibility checks every account the voter is known to be, including any whose address reaches the same inbox | `test_SY7_*`, `test_nobody_votes_for_their_own_team`, `test_signed_in_voters_still_cannot_back_their_own_team` |
| SY8 | Organizers or admins voting (they see the count) | Stopped | Refused in every mode | `test_organizers_and_admins_cannot_vote_but_judges_can` |
| SY9 | A bloc of real people piles onto one project | Capped | Quadratic voting with a per-project ceiling (3 votes of 25 credits): with voters who look at 10 projects, a 10% bloc reached the top 3 in 0% of simulated events, against 59% under one person, one vote (10.3, 10.4) | `test_the_ceiling_is_what_keeps_a_bloc_out_of_the_top_three` |

**Ballot stuffing** (one ballot, or one script, counting for more than it should):

| Id | Attack | Result | How | Evidence |
| --- | --- | --- | --- | --- |
| BS1 | Spend more credits than the budget, more votes than the ceiling, negative, fractional or `true` votes | Stopped | `method.check` and `clean_votes` inside the save, whatever the browser's meter said | `test_the_budget_and_ceiling_hold_on_the_server` |
| BS2 | Vote for a draft, a flagged duplicate or another event's project | Stopped | Only projects on the ballot count | `test_a_flagged_duplicate_drops_out_of_the_count`, `test_the_ballot_lists_what_the_gallery_lists` |
| BS3 | A script opening ballot after ballot | Stopped past 100 an hour per network, flagged below it | `vote.new_ballot` limit; burst and copied-ballot flags | `test_BS3_*` |
| BS4 | Replay or guess a voting link; a mail scanner using it up | Stopped | Tokens are 32 random bytes, stored only as SHA-256, single use by a guarded update, used up only by POST, 30 minutes | `test_only_the_hash_of_the_token_is_stored`, `test_links_expire`; probe `BS4` |
| BS5 | Vote before submissions close or after voting closes | Stopped | The window is checked with the server clock inside the transaction | `test_the_window_is_the_servers` |
| BS6 | Watch the running count to coordinate, or to know how many sock votes are needed | Stopped | No number leaves the portal (not even turnout) until voting has closed and an organizer has published; ballots never reach webhooks | `HiddenResultsTests`; probe `BS6` |
| BS7 | Cast a ballot from another site in a signed-in voter's browser | Stopped | The form needs a CSRF token; the API needs JSON and the portal's own `Origin` | `test_BS7_*`; probe `BS7` |
| BS8 | Buy votes, or ask friends to vote | Open, capped | Nothing in software sees money change hands; the ceiling caps what each bought vote is worth (SY9) | |
| BS9 | An organizer quietly removes the ballots they don't like | Open, logged | Leaving a ballot out needs a reason, is logged, is reversible, and the ballots CSV shows it | `test_rules_lock_once_a_ballot_is_in`, integrity `DecisionTests` |

**Submission scraping** (reading or copying what isn't public yet):

| Id | Attack | Result | How | Evidence |
| --- | --- | --- | --- | --- |
| SC1 | Copy an early team's idea, description or repository while there's still time to use it | Stopped (fixed, 11.4) | Submitted projects stay private to their team and the organizers until the deadline, on every surface: gallery, home page, project pages, both APIs, the embed widget and feed, images, comments. An organizer can choose to show them early | `test_SC1_*`, `test_submitting_a_complete_project_puts_it_in_the_gallery` |
| SC2 | Walk project ids to find drafts, duplicates and held-back projects | Stopped (a leak fixed, 11.4) | Anything not public answers exactly like an id that doesn't exist, on the pages and the API | `test_SC2_*`; probe `SC2`: 40 public, every other id identical to a missing one |
| SC3 | Guess an uploaded image's address | Stopped | Files get random names and are served through a view that applies the project's visibility | `test_SC1_*` (the image answers 404) |
| SC4 | Harvest participants' email addresses | Stopped | No public page, API or feed includes a member's address | `test_SC4_*`; probe `SC4`: 0 addresses |
| SC5 | Read private form answers | Stopped | Questions marked private are shown only to the team and organizers | `test_private_answers_stay_private` |
| SC6 | Read another track's or an unassigned project's judging, or a peer's scores | Stopped | 404 outside a judge's assignments; `?judge=` for a peer is 403 before any lookup | `IsolationTests`; the organizers' checker; probe `JC1` |
| SC7 | Download the whole event (archive, fixtures.json, CSVs) | Stopped | Organizers only | `test_only_organizers_see_the_tab_and_downloads`; probe `SC8` |
| SC8 | Copy the whole public gallery once it's public | Open, by design | It's public. There's no rate limit on reading, and the API and feed make copying easier. Nothing private is in it (SC4, SC5) | |

**Judge collusion** (a judge helping a friend or hurting a rival):

| Id | Attack | Result | How | Evidence |
| --- | --- | --- | --- | --- |
| JC1 | Read other judges' scores or the rankings, to see where a friend stands | Stopped | Backend isolation (5) | `test_asking_for_a_peer_is_refused_by_the_backend`; probe `JC1` |
| JC2 | Pick a friend's project to review | Stopped | Judges can't choose: organizers assign, by hand or by the algorithm (2.3), and anything else answers 404. With 30 judges and 3 reviews a project, a given judge is on a given project's panel about one time in ten | `test_JC2_*` |
| JC3 | Give a friend top marks on your own panel | Flagged | Normalization corrects a judge who's harsh or generous with everyone; it can't tell a targeted boost from a real opinion. The new check (11.4) compares each judge's normalized score with the rest of the panel; the simulation below says what it catches | `test_JC3_*`; 11.7 |
| JC4 | Two judges on one panel both boost a friend | Flagged, weakly | The check still fires in about half of cases, but with 2 of 3 judges agreeing, the honest one is the one who looks out of line. The flag names the project; reading it needs a person | 11.7 |
| JC5 | Judge a team you mentored, without saying so | Open | Judges declare conflicts themselves, and organizers can record them; the portal can't know about a friendship. Staff can't join teams, so a judge is never on a team in their own event | `test_judges_cannot_join_a_team_in_their_event` |
| JC6 | A judge competes too, under a second account | Open | A different address is a different person to the portal | `test_JC6_gap_*` |
| JC7 | Change scores after the fact | Logged | Allowed until judging ends (honest corrections happen); every save is a new `ScoreRevision`, never an edit | `test_second_submission_updates_the_same_ballot_and_keeps_history` |
| JC8 | Tell a team their scores | Open | Nothing stops a judge talking | |
| JC9 | An organizer changes the rubric's weights after scoring to move the winner | Open, logged | Weights stay editable (3), and each change is logged with the old and new weight. Organizers are trusted; the log is how anyone finds out | |

**Deadline gaming** (more time, or a different project, than everyone else):

| Id | Attack | Result | How | Evidence |
| --- | --- | --- | --- | --- |
| DG1 | Submit or edit a second late, by form or either API | Stopped | `events/deadline.py`, checked inside the transaction with the server clock; the deadline instant counts as closed | `test_the_deadline_instant_counts_as_closed`, `test_api_post_is_refused_because_of_the_deadline`; the organizers' checker; probe `DG1`, `DG4` |
| DG2 | Send your own timestamp | Stopped | None is read; `submitted_at` is the server's | |
| DG3 | Start a request before the deadline and finish it after | Stopped, defined | The check and the write happen in one serialized transaction (SQLite `BEGIN IMMEDIATE`), so a save is on time if its check was, and none can slip in behind another | `test_web_form_post_is_refused` |
| DG4 | Submit a placeholder early, finish it after the deadline | Stopped | Every edit is refused after the deadline: fields, images, the thumbnail, withdrawing | `test_withdraw_is_refused`, `test_image_upload_is_refused`, `test_DG8_*` |
| DG5 | Add a strong teammate after the deadline | Stopped | Rosters lock at the deadline | `test_rosters_are_locked` |
| DG6 | Keep pushing to the repository after the deadline | Open | The portal is offline and stores a link, not the code. `submitted_at` and `updated_at` are in the exports, so organizers can compare them with the repository's commit history; asking teams to tag a release at submission makes that easy | |
| DG7 | An organizer reopens the deadline after judging or voting has started, for one team or everyone | Stopped (fixed, 11.4) | Once the deadline has passed and a judge has submitted a score or anyone has voted, it can't move later. Before that it can (a power cut, a typo), and every move is logged | `test_DG7_*`, `test_organizer_extension_reopens_immediately` |
| DG8 | Score after judging ends, vote after voting closes | Stopped | The same window checks | `test_the_window_is_the_servers` |

And the accounts every identity rule rests on: password guessing is locked after 5 wrong passwords per address and 30 per network in 15 minutes (`test_login_throttle_kicks_in_after_five_failures`; probe `A1`), a password change signs out every other device and revokes API tokens, and every form needs a CSRF token.

### 11.4 Found while writing this, and fixed

1. **One inbox, many ballots.** Only the detector knew that `ada+2@x.org` is `ada@x.org`; the ballot didn't. In an email-gated vote, one inbox could confirm `ada+1@`, `ada+2@` and so on, each with its own ballot, only flagged. Email ballots now belong to the inbox (`voting/services.inbox`). Mail still goes to the address as typed. The cost: on the rare provider where `a+b@` and `a@` are different people, they now share a ballot.
2. **Early submissions were public.** A submitted project appeared in the gallery straight away, so a team that finished early handed its idea to everyone still working. Projects now stay private until the deadline (`Event.gallery_before_deadline`, off by default). The brief asks for a public gallery, not a live one, and the checker's event is closed, so it's unaffected.
3. **The deadline could reopen under judges.** An organizer could move a passed deadline later after scoring had started, letting projects change under scores already given. Now refused once any score or ballot exists (`events/services.assert_deadline_can_move`).
4. **Nothing looked for a judge boosting a friend.** The Integrity page now lists projects where one judge's normalized score is 1.5 or more from the mean of the rest of the panel (`judging/engine.panel_gaps`, `integrity/detect.judge_disagreements`, also in the REST API).
5. **A 404 that said too much.** The REST API answered a hidden project with different wording from a missing one, so walking ids told drafts from gaps. Both now give the same answer.

Two claims in this document were wrong and are corrected: 8.1 said network addresses are never stored (the account page's session list keeps each session's address; limits and ballots only keep the salted hash), and 8.2 said `+tags` were dropped everywhere (only in the detector, before fix 1).

### 11.5 What this doesn't stop

1. **Many real inboxes, many real accounts** (SY3, SY4). One person with ten addresses is ten voters in an email-gated vote, and account addresses are never checked, so a signed-in vote is only as strong as the sign-up limit. What helps, in order: restrict an email vote to your organization's domains; keep the community prize small (Devpost's advice, 10.6); read the flagged ballots before publishing. A real fix is identity from outside the portal (single sign-on, phone checks), which an offline laptop can't do.
2. **Open-link votes and private windows** (SY5). A new browser is a new voter. The limits and flags catch a script, not a patient person. Use this mode only when the audience is in the room.
3. **Patient, careful cheats** (8.5). Several devices, several networks, ballots minutes apart and not identical: no sign fires.
4. **Colluding judges** (JC3, JC4). The flag catches 58% of single-judge boosts and flags 2 or 3 honest projects in every 40 while doing it. On the fixture, whose marks are noisier than the simulation's honest judges, it flags 8 of 40. Two judges who agree look like a majority. More reviews per project help most, because one boost moves a mean of 5 less than a mean of 3.
5. **Undeclared conflicts and second accounts** (JC5, JC6). The portal can't know who knows whom.
6. **Code written after the deadline** (DG6). The portal stores a link, not the code.
7. **Scraping what's public** (SC8), **vote buying** (BS8), **judges talking** (JC8). Out of reach for software.
8. **The organizer** (BS9, JC9). An organizer can reweight the rubric, leave ballots out, and choose the judges. Every such action is logged with a name and, where it matters, a reason, and certificates are signed (README, T4) so a result can't be changed quietly after it's handed out. But an organizer who wants to fix a result can. That's the trust model of every hackathon.
9. **A reverse proxy** in front of the portal would put everyone on one network key (11.2). Not a hole in the shipped setup; a trap for anyone who changes it.

### 11.6 A live run

`tests/attacks/probe.py` against a fresh container on an internal network (no internet), with the demo sessions from `.dogfood.toml`:

```
DG1 submit after the deadline (old API)                    STOPPED  403
DG4 edit a submitted project after the deadline (REST)     STOPPED  403
DG8 withdraw after the deadline                            STOPPED  403
JC1 a judge asks for a peer's scores                       STOPPED  403, 403
JC1 a judge asks for the rankings                          STOPPED  403
SC2 walk project ids looking for hidden ones               STOPPED  40 public, 39 not: every other id answers exactly like a missing one
SC4 harvest participants' emails from public pages         STOPPED  0 addresses found
SC8 download the whole event as a participant              STOPPED  403
SY2 one inbox asks for links as mallory+0..+3@             STOPPED  [200, 200, 200, 429]
SY6 fake networks with X-Forwarded-For to keep asking      STOPPED  refused after 20 links from one real network, whatever the header said
BS6 read the running count while voting is open            STOPPED  403 results_hidden, participant 403
BS7 cast a ballot from another site in a voter's browser   STOPPED  cross-origin JSON 403, form post 415
BS4 confirm a voting link without the email                STOPPED  400
A1 guess the organizer's password                          STOPPED  [401, 401, 401, 401, 401, 429, 429]

14 of 14 attacks stopped.
```

The probe only runs attacks that should be refused over HTTP. The open ones (11.5) are demonstrated by the `_gap_` tests in `tests/test_threat_model.py`, which pass because the attack works; if one starts failing, the portal changed and this section needs rewriting. The first run of the probe had two lines marked OPEN, both mistakes in the probe: it asked judge_a for judge_a's own scores (fixture judge `jdg_24`), and it sent the ballot as a participant who hadn't passed the email gate, which the old API refuses (401) before it gets to the cross-site checks.

### 11.7 Judge collusion, simulated

`python src/judging/engine.py --trials 1000 --collusion`: fixture-like events (30 judges, 40 projects, 3 reviews each, whole-number marks, judges with their own harshness and noise, 7% who give everything the same mark). The friend is a random project from the bottom half. The colluding judges are ones the assignment put on the friend's panel, and they give it a 5; everyone else marks honestly.

```
scenario                                     median rank   top 3  top 10  flagged
honest panel (the friend's true place)                31      0%      2%       6%
1 of 3 judges gives the friend a 5                    17      2%     17%      58%
2 of 3 judges give the friend a 5                      7     24%     71%      56%

With nobody colluding, 5.8% of projects are flagged anyway (2.3 of 40 per event): honest disagreement, for an organizer to read.

 threshold  1 colluder caught  2 colluders caught  honest projects flagged
       1.0                86%                 84%                    29.7%
      1.25                75%                 70%                    14.5%
       1.5                58%                 56%                     5.8%
      1.75                40%                 40%                     2.1%
       2.0                25%                 28%                     0.6%
```

What it says:

* **One colluding judge can't win it for a friend**: a bottom-half project climbs from about 31st to 17th, and reaches the top 3 in 2% of events. It's still a real gain, and a prize for "top 10" would be in reach 17% of the time.
* **Two colluding judges on one panel can**: 24% of those events put the friend in the top 3. That's the case normalization doesn't touch, because it's two judges agreeing, not one judge mis-scaled.
* **The flag threshold is a trade-off**, measured rather than picked (the second table): at 1.0 it catches 86% of single boosts but flags 30% of honest projects; at 2.0 it flags almost nothing honest but catches only 25%. At 1.5 it catches 58% and puts 2 or 3 honest projects in every 40 on the organizer's list, which is a list short enough to actually read.
* **The cheapest defence is assignment itself**: with the load-balanced random assignment (2.3), the chance that a particular judge lands on a particular friend's panel is about 3 in 30, and a pair of colluders about 1 in 150.
