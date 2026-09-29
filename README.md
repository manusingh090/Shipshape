# Shipshape

Shipshape is a hackathon portal that runs on one laptop with the network off. People sign up, form teams through invite links, and hand in projects they can keep editing until the deadline, which the server enforces. Everyone can browse the entries in a public gallery. Organizers invite judges, assign projects to them by batch or by algorithm, have them score against a weighted rubric, and get a ranking that corrects for harsh and generous judges. The community can vote as well, by open link, by email or with an account, using quadratic voting with a ceiling so that a group of friends can't decide the result. When it's over, the organizers publish the results, and every team can see its place.

It was built for the DOGFOOD 2026 hackathon, and this release claims **all four tiers**. The organizers' acceptance checker verifies T1 and T2. It has no checks for T3 or T4, so it lists them as "claimed but not verified":

```
claimed T1 T2 T3 T4, verified T1 T2
note: claimed but not verified: T3 T4
```

The checker's complete output is in [acceptance-report.txt](acceptance-report.txt). T3 and T4 are built and tested all the same: the tables in [What it does, tier by tier](#what-it-does-tier-by-tier) say where each of their requirements lives, which tests cover it, and how to try it yourself.

## Contents

1. [What you need](#what-you-need)
2. [Quick start](#quick-start)
3. [What you get after the first boot](#what-you-get-after-the-first-boot)
4. [A guided tour](#a-guided-tour)
5. [Checking the tiers yourself](#checking-the-tiers-yourself)
6. [What it does, tier by tier](#what-it-does-tier-by-tier)
7. [Who can do what](#who-can-do-what)
8. [The fixture's awkward cases](#the-fixtures-awkward-cases)
9. [Bonus challenges](#bonus-challenges)
10. [Known limitations](#known-limitations)
11. [Configuration](#configuration)
12. [Running it without Docker](#running-it-without-docker)
13. [Tests and other tools](#tests-and-other-tools)
14. [Troubleshooting](#troubleshooting)
15. [Frequently asked questions](#frequently-asked-questions)
16. [Repository layout](#repository-layout)
17. [The other documents](#the-other-documents)
18. [License](#license)

## What you need

| | Needed? | Notes |
| --- | --- | --- |
| **Docker with Compose v2** | Yes | Docker Desktop on Windows or macOS, or Docker Engine with the Compose plugin on Linux. The command is `docker compose` (with a space); the old `docker-compose` v1 isn't supported. Built and checked with Docker 29.6 and Compose 5.3 on Windows 11 (x86_64), and from a fresh copy of the repository on a Docker 29.8 with Compose 5.5 that had no network at all and no images. Works on amd64 (Intel and AMD) and arm64 (Apple Silicon, ARM servers). |
| **Port 8080** | Yes, or another port | The portal is published on `localhost:8080`. If that port is taken, pick another (see [Troubleshooting](#troubleshooting)). |
| **About 700 MB of disk and 200 MB of memory** | Yes | The repository is about 95 MB, the image 273 MB, and the build leaves about 300 MB of cache (`docker builder prune` frees it). The running portal uses about 155 MB of memory with its three web workers. |
| **Internet access** | No | Not even for the first build. The base system and every Python package are in the repository, in [src/vendor/](src/vendor/README.md), so nothing is downloaded: see [Is the internet ever needed?](#is-the-internet-ever-needed). You only need a connection to get the repository in the first place. |
| **Python 3.11 or newer** | No | Only if you want to run the acceptance checker or the test suite directly on your machine. Both can also run inside Docker instead. |
| **Git** | No | To clone the repository (about 95 MB, most of it the base system in `src/vendor/`). Downloading and unzipping it works just as well. |

No cloud account, API key, external service or sign-up is needed for any of it.

## Quick start

1. **Start Docker.** On Windows and macOS, open Docker Desktop and wait until it says it's running. To check, run this in a terminal; it should print a version:

   ```bash
   docker compose version
   ```

2. **Get the code and go into its folder.**

   ```bash
   git clone <repository-url> shipshape
   cd shipshape
   ```

   (Or unzip a download and `cd` into the folder that holds `docker-compose.yml`.)

3. **Start the portal.** This one command builds the image the first time (about half a minute, with no internet needed), creates the database, loads the fixture data and starts the web server:

   ```bash
   docker compose up
   ```

4. **Wait for the ready message.** When the terminal shows the lines below, the portal is ready. The session cookies are the ones the acceptance checker uses.

   ```
   fixtures: fixtures.json -> Sample Hack 2026: 8 tracks, 30 judges, 40 teams, 41 projects (1 flagged duplicate: prj_41 repeats prj_07)
   judging: 3 criteria, 126 submitted scores from 30 judges, 8 reviews assigned and not yet started

   seeded. test logins:
     organizer    Cookie: session=org_3c9e51d7a2
     judge_a      Cookie: session=jdg_a_8f14c2e6
     judge_b      Cookie: session=jdg_b_52e0b9d1
     participant  Cookie: session=prt_91d7aa3f
     admin        Cookie: session=adm_6b2f03c8
   ...
   portal: http://localhost:8080
   [INFO] Listening at: http://0.0.0.0:8080
   ```

5. **Open <http://localhost:8080>** in a browser. The gallery and the events are public. To sign in, choose **Sign in** and press one of the **Be ...** buttons (Be Rosa, Be Diego, Be Priya and so on): demo mode signs you in as that person without typing anything. Every seeded account also has the password `dogfood-demo`.

6. **Stop it** with `Ctrl+C` in that terminal. Your data is kept in a Docker volume, so the next `docker compose up` starts where you left off, in seconds.

Useful variations:

| To... | Run |
| --- | --- |
| Run it in the background | `docker compose up -d`, then `docker compose logs -f portal` to watch the log |
| Stop a background run | `docker compose down` (keeps the data) |
| Start again from a clean, freshly seeded database | `docker compose down -v`, then `docker compose up` |
| Use another port, for example 8090 | `PORTAL_PORT=8090 docker compose up` (PowerShell: `$env:PORTAL_PORT=8090; docker compose up`) |
| Rebuild after changing the code | `docker compose up --build` |
| Check it's healthy | `docker compose ps` shows `healthy`; <http://localhost:8080/healthz> answers `ok` |

## What you get after the first boot

### Two events

* **Sample Hack 2026** is the fixture event, loaded from `src/fixtures.json` exactly as the organizers published it: 8 tracks, 30 judges, 40 teams, 41 projects and 126 scores. Its deadline (1 March 2026, 18:00 UTC) has passed, so it's closed to submissions and open for judging. In demo mode the seed also runs one "top up to three reviews" batch, which leaves eight reviews unstarted, so the judging dashboard has something to chase. Its community vote is open for two weeks from the first boot, email-gated, with no ballots yet. Its results aren't published yet.
* **Autumn Build Weekend** is a demo event that opens when the container first boots and closes three days later, so you can walk through forming a team, saving a draft, submitting and editing. It runs on Asia/Kolkata time, to show that each event keeps its own time zone. Its community vote needs an account and opens when submissions close.

### Accounts

Every seeded account signs in with the password `dogfood-demo`, and while demo mode is on the sign-in page has a one-click button for each of these:

| Button | Account | Role | Good for trying |
| --- | --- | --- | --- |
| Be Rosa | organizer@example.org | Organizer of both events | The organizer console: judging, the vote, results, certificates, integrity, import and export |
| Be Diego | diego.herrera@example.org | Judge (fixture `jdg_24`), finished | A judge's queue and scores; being refused other judges' scores and the organizer side |
| Be Jonas | jonas.vogel@example.org | Judge (fixture `jdg_26`), finished | The same, from a second judge, for comparing what each can see |
| Be Ada | ada40@example.org | Judge with reviews still to do | Scoring a project on the scorecard, declaring a conflict of interest |
| Be Priya | priya1@example.org | Participant, captain of NorthKiln | A closed submission (read only), her team's place once results are out, and a team and draft in the open event |
| Be Tom | tom@example.org | Participant with no team yet | Starting or joining a team from an invite link |
| Be Kit | admin@example.org | Platform admin | Changing platform roles, deactivating accounts, the platform audit log |

Every judge and team member in `fixtures.json` has an account too, with the same password. Signed out, you're a visitor: you can browse events, the gallery and published results, and vote in Sample Hack 2026's email-gated vote with any address (demo mode shows the link it would have mailed, because there's no mail server offline).

## A guided tour

Each walkthrough takes a few minutes and starts from <http://localhost:8080>.

### As an organizer (Be Rosa)

1. Choose **Organize** in the top bar, then **Sample Hack 2026**. This is the organizer console; its tabs run along the top.
2. Open **Judging**. **Progress** shows who hasn't started (the top-up batch left some) and which projects are short of judges. It refreshes itself.
3. Open **Rubric** to see the three criteria and their weights, then **Assignments** to see who reviews what. The batch form there plans an assignment round: **Preview** shows the plan, and nothing is saved until you press **Save this batch**.
4. Open **Results**. This is the ranking with normalization applied, and a raw ranking beside it so you can see what normalization changed and why.
5. In the **Publishing the results** panel, tick "Share the judges' feedback with each team" if you like, and press **Publish the results**. This closes judging, so the ranking is final.
6. Open **Community vote**. Press **Close voting now**, then **Publish the results**, to make the vote's count public.
7. Visit the event page and open **Results** to see what everyone else now sees.
8. Try **Certificates** (give a prize, then issue signed certificates), **Integrity** (what the anti-abuse checks flagged), **CSV exports** (one spreadsheet per stage, from teams to the audit log), **Export and import** (the whole event as one .zip) and **Activity** (the audit log).

### As a judge (Be Ada, then Be Diego)

1. As Ada, choose **Judging** in the top bar, then the event. Your queue lists only the projects assigned to you.
2. Open one. Mark it on the scorecard, which shows a live weighted total; **Save draft** keeps it private and **Submit score** makes it count. Or use **I have a conflict of interest** to step away from it.
3. As Diego, try opening another judge's scores by address, for example <http://localhost:8080/api/judge/scores?judge=jdg_01>: the portal answers 403. A project that isn't in your queue answers 404, the same as one that doesn't exist.

### As a participant (Be Priya, then Be Tom)

1. As Priya, open **Sample Hack 2026**. Her team's submission is read only, because the deadline has passed.
2. Once an organizer has published the results (see above), open the event's **Results**: Priya sees her team's place and, if feedback was shared, the judges' average marks and comments, without their names.
3. Open **Autumn Build Weekend**, start a team, save a draft and submit it. Keep editing it: that's allowed until the deadline.
4. As Tom, join a team with an invite link. Priya's team page shows the link to send.

### As a voter (signed out)

1. Sign out, open **Sample Hack 2026** and choose the community vote.
2. Give an email address. Demo mode shows the one-time link it would have mailed; open it and confirm.
3. Spread up to 25 credits: n votes on one project cost n × n credits, and one project can get at most 3 votes from you. The count stays hidden until voting closes and an organizer publishes it.

### As a program (the REST API)

1. Open <http://localhost:8080/api/v1/docs> for the reference, or download the OpenAPI document from <http://localhost:8080/api/v1/openapi.json>.
2. Make a token, then use it:

   ```bash
   curl -X POST http://localhost:8080/api/v1/auth/tokens -H "Content-Type: application/json" \
        -d '{"name": "try", "email": "organizer@example.org", "password": "dogfood-demo"}'
   curl -H "Authorization: Bearer <the token>" http://localhost:8080/api/v1/events/sample-hack-2026/judging/results
   ```

## Checking the tiers yourself

The organizers' checker, `tests/acceptance/run.py`, is included unmodified next to their brief (`tests/acceptance/spec.md`). It needs the portal running as above (it uses the demo session cookies, so `DEMO_MODE=1` must be on, which it is by default). There are three ways to see the result:

1. **Read the committed report**, [acceptance-report.txt](acceptance-report.txt). It was produced with the portal on a Docker network that had no route to the internet at all.

2. **Run the checker with Python on your machine** (3.11 or newer; it uses only the standard library):

   ```bash
   python3 tests/acceptance/run.py .dogfood.toml --fixtures src/fixtures.json
   ```

   On Windows, use `py` or `python` instead of `python3`.

3. **Run the checker with Docker, without installing Python.** From the repository folder, while the portal is running. This borrows the portal's own image, which already has Python, so nothing is downloaded:

   ```bash
   docker run --rm --network "container:$(docker compose ps -q portal)" -v "$PWD:/w:ro" -w /w shipshape-portal:local python tests/acceptance/run.py .dogfood.toml --fixtures src/fixtures.json
   ```

   In PowerShell, write `${PWD}` instead of `$PWD`. In Git Bash on Windows, put `MSYS_NO_PATHCONV=1` in front of the command, and use `$(pwd -W)` for the folder.

Each way should end with `claimed T1 T2 T3 T4, verified T1 T2` and the note `claimed but not verified: T3 T4`, because the checker's probes stop at T2. The seven lines above it are the checks: the gallery is public, a fixture project appears in it, the closed event refuses a late submission, a judge sees their own scores and is refused another judge's, a participant is refused judges' scores, and the CSV export works.

## What it does, tier by tier

### T1: core

| Requirement | Where it lives |
| --- | --- |
| Authentication and sessions | Sign up, sign in, sign out (POST only). Server-side sessions in the database, a cookie named `session`, HttpOnly and SameSite=Lax. The account page lists every browser you're signed in on and can end any of them; changing your password ends the others. Five wrong passwords lock an email for fifteen minutes. |
| A real role model | Visitor (no session), member, platform organizer and platform admin, plus per-event organizer and judge roles, and "participant", meaning on a team in this event. The checks live in `src/events/access.py` and `src/judging/access.py` and run on every request. See [Who can do what](#who-can-do-what). |
| Event creation with dates, tracks and prizes | `/events/new/` for platform organizers. Kickoff, deadline, judging end and results dates, each typed in the event's own time zone and stored in UTC. Tracks, prizes (overall or per track) and custom submission questions are managed in the organizer console. |
| Team formation by invite link | Every team gets a 128-bit invite link. Anyone signed in can join until the team is full. The captain can make a new link, which kills the old one. One team per person per event is also a database constraint. |
| Draft and edit until the deadline | Drafts need only a name and are private to the team and organizers. Submitting checks the rest (tagline, description, track, required organizer questions). Teammates can keep editing until the deadline, and a submitted project can't be emptied out by accident. |
| Deadline enforcement that holds | One module decides, `src/events/deadline.py`, and every write path calls it inside the transaction, against a freshly read event row, using the server clock. That covers the web form, the JSON API, image uploads and deletes, withdrawing, and roster changes. Refused attempts are written to the event's activity log. The tests hit each path after the deadline. |
| Public gallery with search and filter | `/projects/` and `/events/<slug>/projects/`. No sign-in needed. A submitted project joins the gallery when submissions close, so nobody can copy an early team's idea while there's still time (an organizer can choose to show projects as soon as they're submitted). Search matches name, tagline, description, team, tags and track. Filters for event, track and tech tag. Orders: A to Z, newest, oldest, and a shuffle that stays stable while you page through it. |

The submission form has the field set from the brief: name, tagline, long description (safe Markdown), thumbnail, image gallery (up to 8), demo video URL, repository URL, live link, tech tags, track, and the organizer's custom questions, each of which can be required, and public or private.

### T2: judging

It's all in the organizer console under **Judging**, and for judges under **Judging** in the top bar. [JUDGING.md](JUDGING.md) has the reasoning, the formulas and the evidence.

| Requirement | Where it lives |
| --- | --- |
| Judge invitation and assignment, by batch or algorithmically | Add a judge by email or make a single-use invite link, with the tracks they may review (or make them a floater). Assignment runs the engine from the brief's reference implementation: each project gets the eligible judges carrying the fewest reviews, never across a track or a conflict of interest, never a repeat, shuffled so order can't bias it. Run it over everything, or as a batch over one track, chosen projects or chosen judges. Every batch is previewed before it's saved and records its seed, so it can be reproduced. Judges can declare a conflict and step away from a project. |
| Scoring against a weighted, organizer-configurable rubric | Each event has its own criteria, descriptions and weights on one scale. Judges score on a paper-style scorecard with a live weighted total, save drafts, and can revise a submitted score until judging ends; every save is kept in an append-only history. |
| Role isolation, enforced in the backend | Every judge-facing query starts from the signed-in judge. Another judge's scores: 403. A project that isn't in your queue, in any track: 404, the same as a project that doesn't exist. Organizers and admins can't judge, and judges can't compete. Tested with raw requests, not by looking at the UI. |
| Live progress dashboard | Who hasn't started (how much they owe, since when, and their email), who's part way, which projects are short of judges, and each batch's progress. It refreshes itself every 10 seconds and is also available as JSON. |
| Cross-judge normalization, documented and defended | Per-judge z-scores with the spread shrunk toward the population (kappa 5). A judge who gives everyone the same mark, or who scored once, counts as a neutral vote. Tested against known truth over thousands of simulated events and explained on the fixture itself: JUDGING.md, section 4. |
| Publishing the results | Console, Judging, **Results**: an organizer publishes the judges' ranking once submissions have closed. Publishing ends judging there and then, so the published ranking is final and no score can move under it. Options: share each team's feedback with it (its average mark per criterion and the judges' comments, never who wrote them), and announce the winners at the same time. Everyone who can see the event then gets the **results page** (`/events/<slug>/results/`): the winners, the judges' ranking by track, and the community vote once it's published. A team member also sees their own project's place and, if shared, its feedback. Taking the results down hides them again; moving the judging end date into the future takes them down too. |
| CSV export at every stage | Teams, projects, judges, assignments, raw scores, normalized scores, final rankings, score history and the audit log, each from the console's **CSV exports** tab or at `/api/events/<slug>/export/<stage>.csv`. Organizers only, and defused against spreadsheet formula injection. |

### T3: community (claimed; the checker has no T3 checks)

[JUDGING.md](JUDGING.md), section 10, has the reasoning and the evidence.

| Requirement | Where it lives |
| --- | --- |
| Community voting with configurable access: open link, email-gated, or authenticated | Organizer console, **Community vote**. Open link: a secret link the organizer can replace, one ballot per browser. Email-gated: a one-time link to the voter's address (only its hash is stored, and it's used up by a button press, not by opening it, so mail scanners can't burn it), optionally limited to the organizer's own domains. Signed in: one ballot per account. In every mode organizers and admins can't vote, and nobody can back their own team. |
| Something better than one person, one vote | Quadratic voting: n votes on one project cost n² credits from a budget of 25, and one ballot can give one project at most 3 votes. In simulated events, a tenth of the voters voting as a bloc put a weak project in the top three in 59% of events under one person, one vote, and never under this. One person, one vote stays available as an option. |
| Comments on gallery projects | A thread under every listed project. Anyone can read; signed-in people write, with safe Markdown. Team members and organizers are labelled. Judges of the event can't comment, so a judge's opinion never reaches the other judges or the team. Authors remove their own with a "Remove my comment" button, and it's gone for everyone; organizers remove anyone's with a reason, which is logged. Limited to 5 a minute and 30 an hour per person. Organizers can switch comments off per event. Exported as `comments.csv`. |
| Results hidden from everyone but organizers during the voting window | Organizers see the live count. Everyone else, judges and participants included, sees no numbers at all, not even turnout, on the page, in the API (`/api/events/<slug>/vote/results` answers `403 results_hidden`) or in the exports. Following Devpost's advice, closing isn't enough: an organizer reviews the ballots and presses Publish. An organizer can close voting early (**Close voting now**) to publish sooner. Reopening voting takes the results down again. |
| Randomised project ordering on ballots, to kill position bias | Every voter gets their own shuffled order, the same each time they return (seeded by their account, confirmed address or browser session, mixed with the site secret). One shared shuffle wouldn't do: in simulation it handed 94% of top-three places to the first ten projects listed, against a fair 25% with per-voter shuffling, and the best project won 13% of the time against 51%. A to Z stays available as an option. |
| Anti-abuse: rate limits, duplicate detection, an audit trail an organizer can read without a database client | **Rate limits** on sign-ins, sign-ups, submissions, scores, voting links, ballots and comments, all in one table (shown on the organizer's **Integrity** tab), set to stop scripts rather than crowds; each refusal is logged once per burst. **Duplicate detection** flags ballots (bursts from one network, the same device, copied votes, email aliases, accounts made minutes before voting), projects with the same repository or title, the same comment on many projects, and judges whose score for a project stands far from the rest of its panel. It flags, it doesn't punish: an organizer leaves ballots out, hides duplicates or removes spam, always with a reason, always reversible. **The audit trail** is the console's Activity tab, filterable by kind and searchable, plus a platform log for admins. JUDGING.md, section 8. |

Role isolation holds for all of this: a team's own history never shows who judges them, when a judge scored, or a judge's conflict and its reason (those stay in the organizers' activity log).

### T4: stretch (claimed; the checker has no T4 checks)

| Requirement | Where it lives |
| --- | --- |
| A REST API and webhooks covering every action the UI can take | **138 endpoints** under `/api/v1/`, one for every page and every form operation in the portal, published as an **OpenAPI 3.1 document** at `/api/v1/openapi.json` (and in the repository as `src/api/openapi.json`), with a readable reference at **`/api/v1/docs`**. `tests/test_api_parity.py` walks every route and every form operation in the templates and fails if one has no API equivalent. The endpoints call the same services and validate with the same forms as the pages, so the API can't be more permissive than the UI. Authentication is a personal access token (made on the account page or with a password at `POST /api/v1/auth/tokens`; only its hash is stored; a password change revokes it) or the browser's session. **Webhooks**: an organizer (Console, **Webhooks**) or an admin (platform webhooks) points a URL at the audit log, by the same categories it filters by. Deliveries are signed (HMAC-SHA256 over a timestamp and the body), retried with backoff, logged with the answer, and can be re-sent; a webhook that keeps failing switches itself off. Addresses on the server itself are refused. A built-in receiver checks signatures, so it all works offline, and demo mode seeds one. |
| Certificate and record generation | Console, **Certificates**. Organizers give prizes (after the deadline; track prizes stay in their track; public from the results date), then issue in one go: participation certificates for every team with a listed project, winners' certificates, organizers' certificates, and **judges' records**. Each is a printable page and a small hand-built PDF, and each is **signed with Ed25519**, so anyone with the organizers' public key can check it even after the portal is switched off: `python src/records/verify.py record.json --key <key>` needs no Django and no network. The signature code follows RFC 8032 and passes its test vectors. A judge's record says what they did and carries a SHA-256 of their scores, never the marks; the judge and the organizers can open the receipt behind it, which also shows whether the scores changed after issue. Revoking needs a reason and shows to anyone who checks. Demo mode issues the fixture event's records. |
| Signed, publicly verifiable judge participation records | The judges' records above, made easy to find and check: the event page names the key its records are signed with, `/.well-known/shipshape-records.json` publishes it for programs, each record's page says whether it's genuine or revoked, `/verify/` checks a record file, and judges find their own record (and its private receipt) on their queue. |
| An embeddable gallery widget | Console, **Embed**. Two lines on any site (`<div data-shipshape-gallery="slug">` and `<script src=".../embed.js">`) put the event's gallery there, sized to fit; a plain iframe works where scripts aren't allowed, and `/embed/events/<slug>/gallery.json` feeds a home-made widget from any site. It shows what a signed-out visitor sees, whoever is signed in (never drafts, scores or vote counts; winners once public), opens projects in a new tab, and is shuffled on every load by default so no project is always first. Options: track, tag, order, how many, light or dark. Only the widget can be framed; every other page refuses. Organizers can switch it off or allow only named sites, enforced by the browser through the widget's content security policy. |
| Bulk import and export, so an organizer can leave as easily as they arrived | **Out**: Console, **Export and import**. One .zip holds the whole event: `fixtures.json` (the shared DOGFOOD shape, so another portal can load it), `shipshape.json` (everything else: dates, prizes and winners, form questions and answers, tags, links and images, the rubric, assignments, every score and its history, conflicts, the vote and its ballots, comments, the activity log), every CSV export, the images, each certificate as a signed file with the public key, and a README saying what's what. Passwords, tokens, webhook secrets and the signing key never leave. **In**: Organize, **Import one** takes a `fixtures.json`, a `shipshape.json` or the .zip and makes a new, unpublished event; people are matched by email, and new ones get an account with no password yet. Teams and judges can also be added to a running event from a spreadsheet (the portal's own teams.csv and judges.csv work as they are). Every import is previewed first (the whole import runs and is rolled back), the live rules still hold (rosters lock at the deadline, team sizes, staff don't compete, organizers don't judge), and every exception is listed. Exporting the fixture event gives back `fixtures.json`, and importing that gives identical rankings; both are tested. |

## Who can do what

| Can... | Visitor | Member | Participant (on a team) | Judge | Event organizer | Admin |
| --- | --- | --- | --- | --- | --- | --- |
| Browse events, the gallery and published results | yes | yes | yes | yes | yes | yes |
| Start or join a team | | yes | | no (staff) | no (staff) | no (manages every event) |
| Save, submit and edit the team's project | | | until the deadline | | | |
| Vote in the community vote | per the access mode | yes | yes, not for their own project | yes | no (sees the count) | no (sees the count) |
| See drafts and flagged duplicates | | | own team only | | yes | yes |
| Score assigned projects | | | | own queue, own tracks | | |
| See scores | | | own project's feedback, once shared | only their own | every judge's | every judge's |
| Manage the event, judging and publishing | | | | | yes | yes |
| Create events | | | | | with the platform organizer role | yes |
| Change platform roles, deactivate accounts | | | | | | yes |

Organizers can't edit a team's project. Nobody can edit anything after the deadline. An organizer who wants to give everyone more time moves the deadline, which is logged; once judges have scored or anyone has voted, it can't move later any more.

## The fixture's awkward cases

* `prj_41` repeats `prj_07` (same team, same title, same repository). It's imported but flagged as a duplicate, which hides it from the gallery and leaves its four scores out of the rankings. Organizers see it under "Needs a decision" and can swap which of the two is listed.
* Two different teams are both called StillTrail. Team names aren't unique; ids are.
* Thirteen teams have one member. That's allowed.
* The fixture only gives a deadline, so kickoff is set 72 hours before it.
* **jdg_07 gave every project a 4.** Normalization makes those scores neutral votes (z = 0) instead of letting them pull projects toward the middle. The two judges with a single score are neutral for the same reason.
* **Two review batches were never finished**, so projects have anything from two to five reviews. Each fixture score is imported as a completed assignment; the demo seed then tops every project up to three, leaving eight reviews pending. The results page counts "informative" reviews and flags projects ranked on too few.

[DATA-MODEL.md](DATA-MODEL.md) has the full mapping.

## Bonus challenges

* **A normalization proof**: JUDGING.md, section 4, with a worked example, a Monte Carlo run over simulated events with a known truth, and the fixture itself explained.
* **A threat model for voting and submission abuse**: [JUDGING.md, section 11](JUDGING.md#11-threat-model-voting-and-submission-abuse). Sybil votes, ballot stuffing, submission scraping, judge collusion and deadline gaming, attack by attack: what's stopped, what's only capped or flagged, and a plain list of what isn't stopped. Each claim has a test in `tests/test_threat_model.py` (including tests that pin the open gaps, so they can't quietly change), `tests/attacks/probe.py` attacks a running portal over HTTP, and `src/judging/engine.py --collusion` measures what colluding judges gain. Writing it found four holes and a small leak, now fixed.
* **API first**: every action in the UI is in a documented API with a published OpenAPI 3.1 document: 150 operations (the 138 under `/api/v1/` and the 12 older `/api/` routes the checker uses, each pointing at its v1 twin), every one with its parameters, request body, response schema, errors and who may call it, plus the webhook deliveries. The document is generated from the same declarations that route the requests, so it can't describe an endpoint that doesn't exist, and request bodies of endpoints that validate with a Django form are derived from that form. It's held to the truth: during the test run every answer the API gives is checked against the schema the document publishes (objects may not carry undocumented keys), the tests exercise all 138 endpoints, and the run fails if one is skipped. It passes `openapi-spec-validator` as valid OpenAPI 3.1. Schemathesis, which generates requests from a document and checks the answers against it, was also pointed at a throwaway container: its generated GET requests (about 1,000 before the run was stopped by hand) produced no server error, but the run didn't finish, so it isn't claimed as a pass of its schema checks. ARCHITECTURE.md, "The REST API", explains how it works.

## Known limitations

* **The checker can't verify T3 and T4**: it has no probes past T2, so its report lists them as claimed but not verified. The evidence is the test suite and the walkthroughs above.
* Imported certificates aren't re-created: they stay valid with the key that signed them, which the archive includes. Offline verification can't know about a revocation; that needs the portal (or the organizers' word).
* The anti-abuse checks flag ballots for an organizer to judge. A patient cheat with several real devices, networks and inboxes gets through, and so do colluding judges more often than not (JUDGING.md, 11.5).
* Email is only used for email-gated voting, and only if `EMAIL_HOST` points at an SMTP server; offline, messages are written to an outbox folder. Password resets aren't built, and judges are invited by link.
* One rubric per event, not per track (a deliberate choice; JUDGING.md, section 3).
* Images are served by Django itself. That's fine for a laptop-sized event; a big public one should put a reverse proxy in front, which in turn needs trusted-proxy support added first (JUDGING.md, 11.2).
* SQLite is the database. It's the right call for "runs on a laptop" and handles an event of this size comfortably, but the app hasn't been tried on Postgres.

## Configuration

Everything is set with environment variables. `docker-compose.yml` sets the ones the demo needs; for anything else, add them under `environment:` there.

| Variable | Default | Meaning |
| --- | --- | --- |
| `DEMO_MODE` | off (on in compose) | Seeds the demo accounts, the demo event, the top-up batch and the checker's session cookies, and shows the one-click sign-in buttons |
| `DEMO_PASSWORD` | `dogfood-demo` | The password of every seeded account in demo mode |
| `PORTAL_PORT` | 8080 | The host port docker compose publishes the portal on |
| `DATA_DIR` | `src/data` (`/data` in the container) | Where the SQLite database, uploads and generated keys live |
| `FIXTURES_PATH` | `src/fixtures.json` (`/app/fixtures.json` in the container) | The fixture file the seed loads |
| `SECRET_KEY` | generated once per install, kept in `DATA_DIR` | Set it to pin the key yourself |
| `ALLOWED_HOSTS` | localhost names | Host names the portal answers to, comma separated |
| `PORTAL_URL` | `http://localhost:<PORTAL_PORT>` in compose | The address printed on boot and used in links |
| `COOKIE_SECURE` | off | Turn on when the portal is served over HTTPS |
| `WEB_CONCURRENCY` | 3 | Number of gunicorn web workers |
| `EMAIL_HOST` | not set | SMTP server for email-gated voting links. Not set: messages are written to `DATA_DIR/outbox` |
| `EMAIL_PORT`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`, `EMAIL_USE_TLS` | 587, empty, empty, on | The rest of the SMTP settings |
| `DEFAULT_FROM_EMAIL` | `Shipshape <no-reply@shipshape.localhost>` | The sender of those emails |
| `RECORD_SIGNING_KEY` | generated once per install, kept in `DATA_DIR` | 64 hex characters to pin the Ed25519 key that signs certificates, so a reinstall keeps the key you published |
| `WEBHOOK_ALLOW_LOCAL` | on in demo mode, else off | Lets webhooks go to loopback addresses (the built-in test receiver) |
| `SELF_URL` | `http://127.0.0.1:<PORT>` in the container | How the portal reaches itself, for the built-in receiver |
| `WEBHOOK_WORKER` | 1 in the container | Sends webhook deliveries from a background thread in each web process. Elsewhere, run `python src/manage.py deliver_webhooks --loop` |

**For a real event, turn `DEMO_MODE` off.** The fixed session cookies, the shared password and the one-click sign-in exist only so the checker and the judges can get in without signing up. Then create the first admin:

```bash
docker compose exec portal python manage.py createsuperuser
```

## Running it without Docker

For development. Python 3.11 or 3.12. Keep the virtual environment and `DATA_DIR` somewhere sensible (for example, not inside a synced folder such as OneDrive, which can lock SQLite files).

macOS and Linux:

```bash
python3 -m venv .venv
.venv/bin/pip install -r src/requirements.txt
export DEMO_MODE=1 DEBUG=1 DATA_DIR=/tmp/shipshape-data
.venv/bin/python src/manage.py migrate
.venv/bin/python src/manage.py seed
.venv/bin/python src/manage.py runserver 8080
```

Windows (PowerShell):

```powershell
py -m venv .venv
.venv\Scripts\pip install -r src\requirements.txt
$env:DEMO_MODE=1; $env:DEBUG=1; $env:DATA_DIR="$env:TEMP\shipshape-data"
.venv\Scripts\python src\manage.py migrate
.venv\Scripts\python src\manage.py seed
.venv\Scripts\python src\manage.py runserver 8080
```

`seed` is safe to run again: it only adds what's missing, so your changes survive.

## Tests and other tools

```bash
python src/manage.py test                                   # the whole suite: 338 tests
python src/manage.py test tests.test_judging                # one module, or one test by its dotted name
```

The run ends by saying how many API endpoints the tests exercised (all 138), with every answer checked against the OpenAPI document. To run the suite without installing Python, inside a throwaway container made from the portal's image (build it first with `docker compose build`; it has everything the tests need, so this works offline too):

```bash
docker run --rm -v "$PWD:/w:ro" -w /w -e DATA_DIR=/tmp/data shipshape-portal:local python src/manage.py test
```

(The same notes about `$PWD` in PowerShell and Git Bash apply as in [Checking the tiers yourself](#checking-the-tiers-yourself).)

The evidence behind the numbers in JUDGING.md can be regenerated:

```bash
python src/judging/engine.py                             # the normalization worked example
python src/judging/engine.py --trials 1000               # the normalization proof over simulated events
python src/manage.py normalization_proof                 # normalization on the fixture, big moves explained
python src/voting/method.py --trials 1000                # the community vote simulation
python src/voting/method.py --order --trials 1000        # the ballot order simulation
python src/judging/engine.py --trials 1000 --collusion   # the judge collusion simulation (threat model)
python tests/attacks/probe.py .dogfood.toml              # attacks a running portal over HTTP (use a throwaway one)
python src/manage.py openapi                             # rewrites src/api/openapi.json (--check only compares)
python src/records/verify.py record.json --key <hex>     # checks a signed certificate offline
```

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `docker: command not found`, or "Cannot connect to the Docker daemon" | Docker isn't installed or isn't running. Start Docker Desktop (Windows, macOS) or the Docker service (Linux: `sudo systemctl start docker`) and try again. |
| `docker-compose: command not found`, or `unknown flag` errors | You're on the old Compose v1. Use `docker compose` (with a space), which comes with current Docker Desktop and the Linux Compose plugin. |
| "port is already allocated" or "address already in use" | Something else uses port 8080. Run on another port: `PORTAL_PORT=8090 docker compose up` (PowerShell: `$env:PORTAL_PORT=8090; docker compose up`), then open <http://localhost:8090>. The checker's `.dogfood.toml` points at 8080, so for the checker either free 8080 or use the Docker way of running it, which doesn't go through the host port. |
| The build stops with "not found" for a file in `vendor/`, or pip says "No matching distribution found" | The copy of the repository is incomplete: the files in `src/vendor/` are missing or damaged (compare them with the checksums in [src/vendor/README.md](src/vendor/README.md)). Clone or download it again. The build never falls back to downloading them. |
| The build says `python-3.12-slim-arm.tar.bz2` (or another name) is not found | Your processor isn't amd64 or arm64, for example a 32-bit Raspberry Pi. Only those two are included. |
| The page doesn't load right after `docker compose up` | The first boot migrates and seeds before the web server starts. Wait for `Listening at: http://0.0.0.0:8080` in the log, or for `docker compose ps` to show `healthy`. |
| The checker fails with "connection refused" | The portal isn't running, or it's on a port other than 8080. Start it with `docker compose up` on the default port. |
| The checker fails the judge or participant checks | The portal isn't in demo mode, so the session cookies in `.dogfood.toml` don't exist. `DEMO_MODE` must be `1`, which is the default in `docker-compose.yml`. |
| Strange data, or you want the fixture as it was published | Start from a clean database: `docker compose down -v`, then `docker compose up`. |
| Changes to the code don't show | The image was built before your change. Run `docker compose up --build`. |
| `python3` isn't found on Windows | Use `py` or `python`, or run the checker with Docker instead. |
| In Git Bash on Windows, a `docker run -v` command mounts the wrong folder | Git Bash rewrites paths. Put `MSYS_NO_PATHCONV=1` in front of the command and use `$(pwd -W)` for the folder. |
| "Too many wrong passwords" when signing in | Five wrong passwords lock an email for fifteen minutes, by design. Wait, or use the one-click **Be ...** buttons, which don't use a password. |

## Frequently asked questions

### Is the internet ever needed?

No, not even to build the image. A normal Python image build downloads a base image from Docker Hub and packages from PyPI; here both are in the repository, in `src/vendor/`: the official `python:3.12-slim` system as one archive per processor type, and the seven packages as wheels. [src/vendor/README.md](src/vendor/README.md) lists each file, where it came from, its checksum and its license. The Dockerfile unpacks the archive with `FROM scratch` and installs the wheels with `pip install --no-index`, `docker-compose.yml` builds with `network: none` (so a missing file stops the build instead of being quietly downloaded) and sets `pull_policy: never` (so Compose doesn't ask Docker Hub for the image first). This was tested on a Docker with no network and no images at all: `docker compose up`, the acceptance checker and the full test suite all ran. Git and the repository download are the only times you need a connection.

Once running, the portal needs nothing outside its container: no CDN, no web fonts, no hosted database, no external API, no mail server. Its content security policy only allows the portal's own address, so a page couldn't load anything from elsewhere even by mistake. The committed acceptance report was produced with the portal on a Docker network that had no route to the internet.

### Where is my data, and how do I back it up or reset it?

In a Docker volume called `portal-data`, mounted at `/data` in the container: the SQLite database, uploaded images, the generated secret key and the certificate signing key. `docker compose down` keeps it; `docker compose down -v` deletes it, and the next boot seeds everything again from `fixtures.json`. To copy it out: `docker compose cp portal:/data ./shipshape-backup`. To take one event elsewhere, use Console, **Export and import**, which gives you the whole event as a .zip.

### Can I load a different fixtures.json?

Yes. Replace `src/fixtures.json` with yours (the same shape as the one the organizers published), then start from a clean database so it's loaded fresh: `docker compose down -v`, then `docker compose up --build`. An organizer can also import one into a running portal from Organize, **Import one**.

### Why is the fixture event closed, and how do I see submitting and editing?

Its deadline in the fixture is 1 March 2026, which has passed, so the server refuses any change to its projects, as the checker expects. Use the demo event, **Autumn Build Weekend**, which is open for three days from the first boot.

### How do I make a real event, without the demo accounts?

Set `DEMO_MODE` to `0` in `docker-compose.yml`, start from a clean database, create an admin with `docker compose exec portal python manage.py createsuperuser`, give an organizer the platform organizer role on the admin page, and create the event from **Organize**. Participants sign up themselves.

### Where do emails go?

Only email-gated voting sends mail. Without `EMAIL_HOST`, each message is written to a file in `DATA_DIR/outbox` instead of being sent, and in demo mode the voting page also shows the link on screen. Set `EMAIL_HOST` and the other SMTP variables to send real mail.

### Which tier does it reach, without reading the code?

All four are claimed. The organizers' checker verifies T1 and T2: see [acceptance-report.txt](acceptance-report.txt), or run it yourself as described in [Checking the tiers yourself](#checking-the-tiers-yourself). It has no checks for T3 or T4; for those, the tables in [What it does, tier by tier](#what-it-does-tier-by-tier) point to the features and the tests that cover them.

## Repository layout

```
.dogfood.toml            what the checker needs: the portal's address, the claimed tiers, the session cookies, the routes
acceptance-report.txt    the checker's output against this build
docker-compose.yml       the one-command start
README.md                this file
ARCHITECTURE.md          how it's put together, and why
DATA-MODEL.md            the tables, their constraints, and how the fixture maps onto them
JUDGING.md               assignment, the rubric, isolation, normalization and its proof, the community vote, the threat model
LICENSE                  MIT
src/                     the application, and the Docker build context
  Dockerfile, boot.py      the image, and its entrypoint (migrate, seed, serve)
  requirements.txt         the five Python packages
  vendor/                  what the build would otherwise download: the base system and the package wheels
  fixtures.json            the organizers' fixture data, unmodified
  manage.py, portal/       the Django project: settings, URLs, security headers, the test runner
  accounts/ events/ teams/ projects/ judging/ voting/ integrity/ records/ embeds/ transfer/ webhooks/ api/
                           one Django app per area (ARCHITECTURE.md describes each)
  templates/, static/      the pages, one hand-written stylesheet, two small scripts
tests/                   the test suite (338 tests)
  acceptance/              the organizers' checker (run.py) and brief (spec.md), unmodified
  attacks/probe.py         the threat model's HTTP attack script
```

## The other documents

* [ARCHITECTURE.md](ARCHITECTURE.md): how it's put together and why: the stack, the code layout, where permissions and deadlines are enforced, the API and its OpenAPI document, webhooks, certificates, publishing, the embed, import and export, security and storage.
* [DATA-MODEL.md](DATA-MODEL.md): every table, the rules the database enforces on its own, and how `fixtures.json` maps in.
* [JUDGING.md](JUDGING.md): how judges are assigned, how scores are weighted and normalized (with the proof), how isolation is enforced, how the community vote works and why, and the threat model.

## License

MIT, see [LICENSE](LICENSE).
