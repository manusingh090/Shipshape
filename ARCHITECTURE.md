# Architecture

This document is for someone who wants to understand, review or change the code: how Shipshape is put together, where each rule lives, and why it was built this way. To run it, start with the [README](README.md); for the tables, see [DATA-MODEL.md](DATA-MODEL.md); for how judging and voting work, see [JUDGING.md](JUDGING.md).

**Contents**

* [Overview](#overview)
* [Why this stack](#why-this-stack)
* [Code layout](#code-layout)
* [How a request is handled](#how-a-request-is-handled)
* [Where permission checks live](#where-permission-checks-live)
* [Deadline enforcement](#deadline-enforcement)
* [Sessions and sign-in](#sessions-and-sign-in)
* [The JSON API and CSRF](#the-json-api-and-csrf)
* [Judging](#judging)
* [Community voting](#community-voting)
* [Comments](#comments)
* [Anti-abuse](#anti-abuse)
* [The REST API](#the-rest-api)
* [Webhooks](#webhooks)
* [Certificates and records](#certificates-and-records)
* [Publishing results](#publishing-results)
* [The embeddable gallery](#the-embeddable-gallery)
* [Import and export](#import-and-export)
* [Uploads](#uploads)
* [Security headers and the offline guarantee](#security-headers-and-the-offline-guarantee)
* [Seeding and demo mode](#seeding-and-demo-mode)
* [Storage](#storage)
* [If this had to host a big public event](#if-this-had-to-host-a-big-public-event)

## Overview

Shipshape is one Django application in one container, with SQLite on a volume. Pages are rendered on the server; a small script adds countdowns, local-time tooltips and copy buttons, and every page works without it.

```
browser --HTTP--> gunicorn (3 workers) --> Django --> SQLite (WAL) in /data
                                             |
                                             +------> uploads in /data/media
```

## Why this stack

The brief rewards the boring parts done well, and the portal has to run on a laptop with the network off.

* **Django** ships password hashing, sessions, CSRF protection, forms and migrations. Those are exactly the parts of a submission platform where hand-rolled code goes wrong.
* **SQLite** means one container and no database server to start, wait for or misconfigure. With WAL mode and `BEGIN IMMEDIATE` transactions it handles a hackathon's traffic comfortably, and writes are serialised, which makes the deadline and team-size checks easy to reason about.
* **Server-rendered templates and hand-written CSS** keep the front end dependency-free. There is no build step, no CDN and no web font, so nothing can fail to load offline.
* **Five runtime packages**: Django, gunicorn (the web server), whitenoise (static files), Pillow (checking and re-encoding uploaded images) and tzdata (time zones, which a slim image doesn't ship). Nothing else is installed.

## Code layout

The application lives in `src/`; the test suite in `tests/`, beside it. Paths in the rest of this document are relative to `src/` unless they start with `tests/`.

| Path | What's in it |
| --- | --- |
| `src/portal/` | Settings, URL root, security headers middleware, error views |
| `src/accounts/` | The user model, sign in and sign up, session tracking, login throttling, the admin page, demo accounts |
| `src/events/` | Events, tracks, prizes, custom questions, event roles, the activity log. Also the three modules everything else leans on: `access.py` (who may do what), `deadline.py` (whether anything may change) and `seeding.py` (loading the fixture) |
| `src/teams/` | Teams and memberships. `services.py` holds every roster rule |
| `src/projects/` | Projects, images, answers, tags. `services.py` saves submissions, `images.py` checks uploads, `api.py` is the JSON API, `views.py` the gallery and forms |
| `src/judging/` | T2. `engine.py` is the pure-Python rubric, assignment and normalization engine (runnable on its own); `access.py` is the isolation layer; `assigning.py`, `scoring.py`, `results.py`, `publishing.py`, `staff.py` and `exports.py` are the services around it; `views.py` is the organizer side, `judge_views.py` the judge side, `api.py` the JSON and CSV endpoints |
| `src/integrity/` | Anti-abuse. `limits.py` is every rate limit in one table plus once-per-burst refusal logging (`Throttle` counts actions that have no table of their own); `detect.py` finds suspicious ballots, duplicate projects and repeated comments; `services.py` is what an organizer can do about them; `views.py` the console's Integrity tab |
| `src/api/` | The REST API. `core.py` is the plumbing (token or session authentication, the endpoint table, errors, paging); `endpoints/` has one module per area; `serializers.py` the JSON shapes; `schema.py` their JSON Schemas, a small validator and form-to-schema conversion; `openapi.py` the OpenAPI 3.1 document; `docs.py` the index and the reference page, all generated from the endpoint table; `models.ApiToken` personal access tokens |
| `src/records/` | Certificates and records. `ed25519.py` signatures (RFC 8032, plain Python); `signing.py` the portal's key and signing; `pdf.py` a small PDF writer; `render.py` the certificate layout; `services.py` awards, issuing, revoking and a judge's receipt; `verify.py` a standalone offline checker |
| `src/transfer/` | Bulk import and export: `export.py` (fixtures.json, shipshape.json and the .zip), `importer.py` (a whole event in), `csvimport.py` (teams and judges from a spreadsheet), `views.py` (Organize, Import an event, and the console's Export and import tab). No models |
| `src/embeds/` | The embeddable gallery: `services.py` (what it shows, where it may be framed, the snippets), `views.py` (the widget page, the loader script, the JSON feed, the console tab). The loader is `static/js/embed.js`; the widget reports its height with `static/js/embed-frame.js` |
| `src/webhooks/` | Webhooks. `delivery.py` queues from the audit log, signs, sends and retries; `worker.py` the background sender; `services.py` managing them; `views.py` the console tab, the admin page and the built-in receiver |
| `src/voting/` | T3 community vote. `method.py` is the pure-Python quadratic and single-vote maths plus the simulation behind the defaults (runnable on its own); `services.py` holds every rule (who a voter is, eligibility, the budget, email links); `results.py` counts; `views.py` has the ballot, its gates and the organizer page; `api.py` the JSON ballot; `exports.py` two CSV stages |
| `src/templates/`, `src/static/` | Markup, one hand-written stylesheet (`static/css/site.css`), and three small scripts: `site.js` (countdowns, local-time tooltips, copy buttons; every page works without it), `embed.js` and `embed-frame.js` (the embeddable gallery) |
| `tests/` | 338 tests, grouped by behaviour: deadline, teams, submissions, access, auth, gallery and seed; for judging the engine, isolation, scoring, assignment, invites, exports and the checker's probes; for voting the method, each access mode, eligibility, the window, hidden results, ballot order and the exports; comments and what a team's history may show; for anti-abuse the limits, each detector, the organizer's decisions and the audit filters; for T4 every area of the REST API, the UI-to-API parity check, and webhook queueing, signing, retries, destinations and the receiver; for records the RFC 8032 vectors, the PDF's structure, the award rules, issuing, judge privacy, revocation and the offline verifier; for embedding what the widget shows, where it may be framed, its options, feed and settings; for import and export the fixture and archive round trips, previews, refused files and the spreadsheet rules; and the threat model, one test per attack in JUDGING.md 11, including the open gaps. `tests/attacks/probe.py` is not a test module: it attacks a running portal over HTTP |
| `src/boot.py`, `src/Dockerfile` | Container entrypoint (migrate, seed, exec gunicorn) and the image. `src/` is the Docker build context, so `src/fixtures.json` travels with the code |
| `src/vendor/` | Everything the build would otherwise download: the `python:3.12-slim` root filesystem for amd64 and arm64, and the Python packages as wheels. Third-party files, unmodified; its README has their sources, checksums and licenses |
| `tests/acceptance/` | The organizers' `run.py` (the acceptance checker) and `spec.md`, unmodified |

## How a request is handled

Every request goes the same way, whichever page or API endpoint it's for:

1. **gunicorn** hands it to Django. Middleware (`portal/middleware.py`) adds the security headers to the answer on the way out: a content security policy that allows only this site, and a refusal to be framed.
2. **The session** is read from the `session` cookie (`accounts/middleware.py` also keeps the list of signed-in browsers up to date). An API request may carry a Bearer token instead (`api/core.authenticate`).
3. **The view** works out who's asking, once: `events/access.Viewer` says whether they're an admin, an organizer, a judge or a participant in this event. Judging views ask `judging/access.py`, which only ever looks at the signed-in judge's own assignments.
4. **A service** does the work (`projects/services.py`, `judging/scoring.py`, `voting/services.py` and so on). A write opens a transaction, re-reads the event row, asks `events/deadline.py` whether the window is open by the server's clock, checks the rules, and writes.
5. **The activity log** gets a line (`events/models.log_activity`). Once the transaction commits, webhooks that listen for it are queued.
6. **The answer** is a rendered page or JSON. A refusal is a message on the page, or `{"error", "detail"}` with its status in the API.

Views are thin. They load things, ask `access.py`, call a service, and render. The services own the rules, so the web form and the JSON API can't drift apart: both end up in `projects/services.save_submission`, and every score, from the scorecard or the API, goes through `judging/scoring.save_score`.

## Where permission checks live

The spec's point about "hiding a button is not refusing a request" is the design rule here.

* `events/access.Viewer` works out, once per request, what the current user is in one event: admin, organizer, judge, participant or none of those.
* Every organizer view calls `require_organizer(viewer)`, which raises a 403. The tests post to console URLs as a participant and a judge and check nothing changed.
* Unpublished events, drafts and flagged duplicates return **404** to people who can't see them, so their existence doesn't leak. So do submitted projects before the deadline (unless the organizers show them early): `projects/views.listed_projects` and `events/access.can_view_project` are the one rule, and the gallery, home page, APIs, embed, images and comments all go through them. The 404 is the same answer a missing id gets, on the pages and the API.
* Uploaded images go through a view that looks up the owning project and applies the same visibility rule. A draft's screenshot is refused to strangers even if they guess the file name.
* Staff can't compete: organizers, judges and platform admins are refused when they try to start or join a team in the event, and organizers can't add someone who is already on a team as staff.
* Anyone who can see every score can't judge: organizers of the event and platform admins are refused as judges, a judge can't be made an organizer of that event, and a judge can't be promoted to admin while judging.
* Judging data has its own layer, `judging/access.py`. Every judge-facing query starts from "rows where judge = the signed-in user"; the assignment table decides whether a judge may open a project at all, and anything outside it is a 404. A request for another judge's scores is a 403, answered before any lookup. JUDGING.md, section 5, has the details.

## Deadline enforcement

`events/deadline.py` is the single source of truth. `Event.phase(now)` returns upcoming, open or closed; the deadline instant itself counts as closed.

Every write that touches a project or a roster does this:

1. open a transaction (SQLite takes the write lock straight away because of `BEGIN IMMEDIATE`),
2. re-read the event row, so an organizer's last-second extension or cut-off is seen,
3. call `assert_accepting_edits` or `assert_team_changes_allowed` with the server's clock,
4. write, and commit.

If the check fails the transaction rolls back and the refusal is logged afterwards, outside the rolled-back transaction, so the organizer can see who tried what and how late. The browser's countdown is display only.

The paths that go through this: the submission form (save and submit), withdrawing to draft, the JSON API, image upload and delete, and creating, joining, leaving, renaming and resetting the invite of a team. `tests/test_deadline.py` visits each one after the deadline.

## Sessions and sign-in

* Sessions are stored in the database. The cookie is called `session`, is HttpOnly and SameSite=Lax, and can be marked Secure with `COOKIE_SECURE=1`.
* `accounts.UserSession` mirrors each session with its device and last-seen time, so the account page can list where you're signed in and end any of them. Changing your password ends every other session; deactivating an account ends all of them.
* Passwords are hashed with Django's PBKDF2 and must be at least ten characters, not common, not all digits and not close to your name or email.
* `accounts/throttle.py` locks an email for fifteen minutes after five wrong passwords, and an IP address after thirty. The counts live in the database so all gunicorn workers agree.
* Signing in rotates the session key (no session fixation) and `next` redirects are checked against the current host (no open redirect).

## The JSON API and CSRF

`/api/events/<slug>/submission` (GET and POST) and `/api/projects` (GET) sit on the same services as the pages.

The POST endpoint is exempt from Django's CSRF token because API clients can't fetch a token first. It stays safe from cross-site requests another way: it only accepts `Content-Type: application/json`, which an HTML form on another site cannot send without a CORS preflight that this app never answers; it rejects any `Origin` header that isn't this site; and the session cookie is SameSite=Lax. The HTML forms keep Django's normal CSRF tokens.

The order of checks matters for honesty: signed in, then valid JSON, then **the deadline**, then team membership, then field validation. A late request is refused as late, whatever else is wrong with it. That's why the acceptance checker's probe gets `403 submissions_closed`, not a CSRF or validation error.

## Judging

The judging module follows the same pattern as submissions: the rules live in services, views stay thin, and nothing derived is stored.

* **Engine.** `judging/engine.py` holds the rubric maths, the assignment algorithm and the shrinkage z-score normalization, in plain Python with no Django import, ported from the reference implementation that came with the brief. It can be run on its own (`python src/judging/engine.py --trials 1000`), which is how the normalization proof in JUDGING.md is produced, and it is unit-tested without a database.
* **Assignment.** `assigning.plan_batch` turns the current rows (existing assignments, judges' tracks, conflicts) into engine input and returns a proposal without saving anything. `run_batch` does the same inside a transaction and saves it, with the seed, so a preview and its commit match. Batch and algorithmic mode are the same call with a different scope.
* **Scoring.** `scoring.save_score` re-reads the event, checks the judging window (it opens when submissions close and ends at the event's judging end), passes the assignment gate, re-checks the judge's track, rate-limits, validates the marks against the event's scale, writes the score and appends a `ScoreRevision`.
* **Results.** `results.compute_results` builds the per-judge raw weighted scores from the marks and the current weights, normalizes them with the event's kappa, and ranks the listed projects, on every request. At a few hundred scores it takes milliseconds, and there is no cache to go stale when a weight changes. `compute_progress` is the same idea for the dashboard, which re-fetches a server-rendered fragment every 10 seconds.
* **Exports.** One builder per stage in `exports.py`, all served from `/api/events/<slug>/export/<stage>.csv` to organizers only.

## Community voting

The same pattern again. `voting/services.save_ballot` opens a transaction, re-reads the event and its voting settings, checks the voting window with the server clock (`events/deadline.assert_voting_open`), checks eligibility, validates the ballot against the method (`voting/method.check`), then replaces the ballot's lines. The ballot page and `/api/events/<slug>/ballot` both go through it.

Who a voter is depends on the access mode, and `services.voter_for` works it out from the request: the account when voting needs one, a confirmed email address carried in the session (for someone signed in, only their own account's address counts), or, for an open link, the session's own ballot key once the secret token in the address has been checked. Eligibility (organizers and admins can't vote, nobody backs their own team) is checked against every account the voter is known to be, including whoever is signed in, so an organizer can't slip in through an email ballot.

Email-gated voting needs mail. Settings use SMTP when `EMAIL_HOST` is set and Django's file backend (`DATA_DIR/outbox`) when it isn't, so nothing tries to reach the network by default. Link tokens are stored hashed and used up by a POST, never a GET.

The count (`voting/results.compute_tally`) is recomputed from the ballot lines on every request, like the judging results. Who may see it is one function, `voting/services.can_see_results`: organizers always; everyone else only once voting has closed and an organizer has published. The results page, `/api/events/<slug>/vote/results` and the event page all ask it, and the CSV exports are organizer-only anyway. Saving the settings so that voting is open again unpublishes.

What order a voter sees the projects in is `voting/services.ballot_for`: shuffled with `method.shuffled` from a per-voter seed (an HMAC of the event and the voter's account, confirmed address or open-link session token, keyed with the secret), so it's different for every voter and the same for one voter every time. The ballot page and the ballot API both use it; the count and the save don't care about order.

## Comments

`projects/comments.py` holds every rule; the views only call it. A thread exists for listed projects only, writing needs an account and an event with comments on, and judges of the event are refused (their opinions are judging data). Removal is soft (`removed_at`, `removed_by`, `removal_reason`), so an organizer's removal is on record and the public thread shows a placeholder. Bodies go through the escape-first Markdown renderer.

Teams see their own history on the team and submission pages. `events/models.team_history` is the only way those pages read it, and it drops judging and voting entries (`score.`, `judge.`, `judging.`, `voting.`), which name judges or carry a conflict's reason. JUDGING.md, section 10, explains the method and the simulation that chose its defaults.

## Anti-abuse

Every write path checks its limit from `integrity/limits.LIMITS` before its transaction opens (the refusal's audit line would otherwise be rolled back with it). Where the model already records the action (comments, score revisions, email links, ballots), the caller passes that count; otherwise `Throttle` rows are counted. `note_refusal` writes one `rate.limited` activity line per key per window.

Detection (`integrity/detect.py`) only flags. Organizer decisions (`integrity/services.py`) need a reason and are logged: leaving ballots out (`Ballot.excluded_at`, which `voting/results.compute_tally` skips), hiding a duplicate project (`duplicate_of`), removing repeated comments.

The audit log is `events.Activity`. Its `event` can be empty for platform-wide entries (sign-in lockouts, sign-up limits, admin account changes), shown at `/admin/audit/`. `events/audit.py` holds the categories and search both log pages use.

## The REST API

Every endpoint is declared once, with `@endpoint(method, path, who=..., summary=..., ui=...)`, into one table (`api/core.ENDPOINTS`). The URL patterns under `/api/v1/`, the JSON index at `/api/v1/` and the HTML reference at `/api/v1/docs` are all built from it, so the documentation can't drift from what's served. `ui` names the page or button the endpoint does the same job as; `tests/test_api_parity.py` maps every route in the site and every form `op` in the templates to endpoints, and fails when one is missing, so a new page can't quietly lack an API.

Endpoints are thin, like the views. They call the same services (`events/services.py`, `accounts/services.py`, `teams/services.py`, `projects/services.py`, `judging/staff.py`, `voting/services.py`, ...) and validate with the same Django forms, fed JSON instead of POST data. A PATCH starts from the form's current values, so a client sends only what changes. Where an older JSON route already existed (the submission, the ballot, scores, progress, results, CSV exports), v1 hands the request to it rather than copying it. Building the API meant moving the logic that was still inline in the console views into services first; the views and the API now share it.

### The OpenAPI document

Each `@endpoint` also declares what it takes and returns: `request=` (the JSON body), `multipart=` (uploads), `query=`, `returns=` and `produces=` (for CSV and zip downloads). They're JSON Schemas written with the helpers in `api/schema.py`, named in a block at the top of each endpoint module, and the shared shapes (one per serializer: Event, Project, Team, Criterion...) are components. An endpoint that validates with a Django form declares `request=sc.form_schema(TheForm, ...)`: the schema is built from the form itself (types, required fields, lengths, choices, help text as the description), when the document is built rather than at import, because many forms add fields in `__init__` from the event. `api/openapi.py` turns the table into OpenAPI 3.1: paths under `/api` (`/v1/...`, with the older routes beside them, each pointing at its v1 twin), parameters from the URL converters, paging parameters for paged lists, bearer and cookie security, the error responses that apply to each operation (all sharing one Error schema), and a `webhooks` entry for the deliveries. It's served at `/api/v1/openapi.json` and written to `src/api/openapi.json` by `manage.py openapi`; a test fails when the committed copy is stale.

The document is checked against the running code, not trusted. The test runner (`portal/testing.Runner`) switches on `api/core._check_contract`: every answer an `/api/v1/` endpoint gives during the tests is validated against its declared schema (errors against Error), with a small validator in `api/schema.py`. Objects refuse keys they don't list, so a field added to a serializer without documenting it fails the tests that return it. `tests/test_openapi.py` drives every endpoint through a realistic event (the older routes are validated there by hand, since they don't pass through `api/core`), and a full run fails if any endpoint was never exercised. Writing the schemas this way found five places where the first draft of the documentation was wrong (deletes return the name of what went, not `true`; the judge's queue says `todo`, not `pending`), which is the point. Outside the tests: `openapi-spec-validator` accepts the document as OpenAPI 3.1, and Schemathesis (property-based: it generates requests from the document) was pointed at a throwaway container: its generated GET requests (about 1,000 before the run was stopped by hand) produced no server error. That run didn't finish, so it isn't claimed as a pass of its schema checks.

Authentication: `Authorization: Bearer ss_...` (a personal access token: only its SHA-256 is stored, it acts as its owner, and it stops working when revoked, when its owner is deactivated, or when they change their password), or the session cookie. With the cookie, a POST must be JSON and a foreign `Origin` is refused, like the older routes; PATCH, PUT and DELETE can't be sent cross-site without a preflight; uploads need a token. The services' rule errors come back as `{"error", "detail"}` with their own status, a form's as 400 with `fields`, rate limits as 429. API writes have their own limit on top of each action's.

## Webhooks

Webhooks are fed by the audit log: `events.models.log_activity` calls `webhooks.delivery.queue_after_commit`, so every entry, once its write commits, becomes a delivery for each active webhook of that event (or, for platform entries, each platform webhook) that listens to its category. A rolled-back action is never announced; a refusal (logged outside any transaction) is. Ballots aren't logged, so votes never leave the portal.

A delivery is a JSON POST signed with `Shipshape-Signature: t=<time>,v1=<HMAC-SHA256 of "<time>." + body>`, with a unique `Shipshape-Delivery` id. Each web process runs a daemon thread (`webhooks/worker.py`, started when `WEBHOOK_WORKER=1`, which `boot.py` sets) that sends what's due every two seconds; a delivery is claimed with a guarded update, so three gunicorn workers never send one twice. Retries follow 1, 5, 30, 120 and 720 minutes, then give up; five given-up deliveries in a row switch the webhook off, logged. Before sending, the host is resolved and loopback, link-local (cloud metadata), multicast and unspecified addresses are refused; private LAN addresses are allowed, since an offline event's receiver is usually there. `WEBHOOK_ALLOW_LOCAL` (on in demo mode) lets deliveries reach the portal's own receiver at `/webhooks/<id>/receive`, which checks signatures exactly as a receiver should.

## Certificates and records

A record is a statement frozen when it's issued: `Record.payload` is the exact JSON that was signed (who, what, which event, when, a code), and `signature` is Ed25519 over that JSON encoded canonically (sorted keys, no spaces, UTF-8). The signing key is 32 random bytes made on first use in `DATA_DIR`, or pinned with `RECORD_SIGNING_KEY`; its public half is on `/verify/`, at `/api/v1/records/key` and in every signed file. Public-key signatures are the point: a hackathon's portal usually runs for a weekend, and a certificate that can only be checked by visiting it stops being checkable when the laptop is closed. `records/verify.py` checks a downloaded file with nothing but the standard library.

The standard library has no Ed25519 and the stack takes no new packages, so `records/ed25519.py` implements RFC 8032 section 5.1 directly (extended coordinates, a few milliseconds a signature) and the tests hold it to the RFC's published vectors. Certificates are PDFs made by `records/pdf.py`: one page, the three standard fonts every reader carries (nothing embedded), text measured with the fonts' published widths so it can be centred and wrapped. The HTML page shows the same payload and prints the same way.

The key is published where people and programs look for it: on `/verify/`, on the event page once it has issued records, and at `/.well-known/shipshape-records.json` (readable from any site). Judges' scores are private (section 5 of JUDGING.md), so a judge's record doesn't list them: it carries a SHA-256 of the judge's submitted scores in a fixed order, and `Record.private` keeps the list it was made from. The receipt page, open only to that judge and the organizers, shows the list, whether it still hashes to the signed value, and whether the judge's scores have changed since. Winners come from `events.Award` (a prize given to a project), public once the event's results date passes. Issuing is idempotent; revoking keeps the record, and a check on the portal reports it revoked with the reason.

## Publishing results

`judging/publishing.py` owns it. The only thing stored is the decision (`JudgingConfig.results_published_at`, `results_published_by`, `share_feedback`); the public ranking is computed per request by `results.compute_results`, like the organizers' one. `state()` is not_ready (submissions open), unpublished or published, and published also requires judging to be closed, so the rule can't be broken by a date edit: `publish()` closes judging if it's open (logged as a moved date), and `events/services.update_event` takes the results down if judging opens again. The results page (`judging.views.public_results`) and `GET /api/v1/events/{slug}/results` both render `publishing.page(viewer, event)`, so they show the same things to the same people: the ranking only when public (or to organizers, as a preview), awards by the existing `records.services.public_awards` rule, the vote by `voting.services.can_see_results`, and to a team member their own place and, when shared, `publishing.feedback()`: averages per criterion and comments in a fixed shuffled order, never a judge's name.

## The embeddable gallery

Every page sends `frame-ancestors 'none'` and `X-Frame-Options: DENY`. The widget page (`/embed/events/<slug>/gallery`) is the one exception: it's exempt from X-Frame-Options and sets its own policy with `frame-ancestors 'self'` plus either `*` or the organizer's allowed sites (`embeds.EmbedSettings`), so the browser refuses to show it anywhere else. It's safe to frame because it has no forms and nothing to click but links, which open in a new tab (`<base target="_blank">`), and because it always renders as a signed-out visitor (`request.user` is replaced), so a signed-in organizer's drafts can't appear on someone else's page.

The loader (`/embed.js`) finds `div[data-shipshape-gallery]` elements, puts a sandboxed iframe in each, and listens for one message type, `shipshape:height`, only from the portal's origin and only from its own frames, to size them. The frame measures its content (not the document, whose height is never less than the frame's) and reports it on load and whenever it changes. The JSON feed sends `Access-Control-Allow-Origin: *` and ignores cookies, so it's public data only. Projects come from the gallery's own search (`projects/views.search_projects`) and are shuffled per load by default, for the position-bias reason in JUDGING.md 10.5.

## Import and export

An export is built from the database on request and nothing is cached. `export.fixture` writes the DOGFOOD shape (records keep their `external_id`, and ones made here get ids like `prj_<pk>`), so the fixture event exports back to `fixtures.json`. `export.archive` writes `shipshape.json` (format `shipshape-archive`, version 1): every row an organizer can see in the console, people by email, and nothing secret.

An import always creates a new, unpublished event, so it can't overwrite one, and the importer becomes its organizer. It runs in one transaction; a preview runs the same code and raises at the end so everything rolls back, which makes the preview report exactly what the real import will do. Images are the one thing a rollback can't undo, so a preview counts them but writes none; the real import re-encodes each through the same Pillow path as an upload. The portal's rules win over the file, and each exception becomes a line in the report: staff aren't put on teams, a team's second listed project is kept as a flagged duplicate, a project submitted after the deadline arrives as a draft, organizers aren't added as judges. Certificates aren't imported, since they're signed by the old portal's key. Uploads are capped (200 MB, 5000 files, 50 MB of JSON) and zip entries whose paths climb out are ignored.

Spreadsheet imports add to a running event through the same checks as adding one person by hand (`assert_team_changes_allowed`, team size, one team per person, `judging.staff.add_judge`); a bad row is skipped and reported and the rest go in. The pending upload waits in `DATA_DIR/imports/` (whole events) or the session (spreadsheets) between the preview and the confirm.

## Uploads

`projects/images.py` opens every upload with Pillow, rejects anything that isn't a readable JPEG, PNG, WebP or GIF under 5 MB, caps the pixel count (decompression bombs), fixes orientation, shrinks it to 1600 px, and writes a fresh JPEG (or PNG when there's real transparency). Re-encoding strips EXIF data such as phone GPS coordinates and means the server only ever serves bytes it wrote. Files are served with `nosniff` and a sandboxing CSP.

## Security headers and the offline guarantee

Every response carries a content security policy of `default-src 'self'` with no inline scripts or styles, `frame-ancestors 'none'`, `form-action 'self'` and `object-src 'none'`, plus `X-Frame-Options: DENY`, `nosniff`, a same-origin referrer policy and a restrictive permissions policy. Because nothing may load from another origin, a template that accidentally pulled in a CDN script or font would break visibly in development instead of quietly depending on the network.

The CSP is also why track colours and the timeline position are CSS classes rather than inline styles.

The build is offline too, because the brief asks for `docker compose up` to work with the network off, and on a fresh machine that includes building the image. The Dockerfile has four stages. `base` starts `FROM scratch` and `ADD`s `vendor/python-3.12-slim-${TARGETARCH}.tar.bz2`, the root filesystem of the official image for the machine's processor (the way the official Debian images are built). bzip2 because Docker decompresses it itself, where xz would need an `xz` program on the Docker host. `packages` installs the wheels with `pip install --no-index --find-links`, into a prefix that the final stage copies. `source` copies `src/` and deletes `vendor/`, so the 90 MB of vendored files never reach the image. The final stage puts the two together and collects the static files. There's no `# syntax=` line, which would fetch a frontend image. Compose builds with `network: none`, so anything missing fails the build rather than being downloaded, and `pull_policy: never`, because otherwise the first `up` asks Docker Hub for `shipshape-portal:local` before building. The whole path was tested in a Docker-in-Docker container with no network and an empty image store: build, boot, the acceptance checker and the test suite, on amd64 and (under emulation) arm64.

## Seeding and demo mode

`python src/manage.py seed` runs on every boot, from `boot.py`. It matches fixture records by id and only creates what's missing, so edits made in the portal survive restarts. With `DEMO_MODE=1` it also creates the organizer, admin and newcomer accounts, the open demo event, and the fixed session rows the acceptance checker uses. With demo mode off it removes those session rows again. Every seeded account shares one password hash so the first boot takes about two seconds instead of a minute.

## Storage

Everything that changes lives in `DATA_DIR` (`/data`, a named volume): the SQLite file, uploads, and a secret key generated on first boot. Nothing secret is baked into the image. Backing up an event means copying that volume; `docker compose down -v` throws it away.

## If this had to host a big public event

* Put nginx or Caddy in front for TLS and to serve `/media/` and `/static/` directly.
* Move to Postgres. The code already takes row locks (`select_for_update`) where Postgres would need them; SQLite simply ignores them because its writes are serialised.
* Set `EMAIL_HOST` to a real SMTP server so email-gated voting links are actually delivered, and use it for password resets and judge invites too (both are links to copy today).
