# Data model

This document describes every table Shipshape stores, the rules the database itself enforces, and exactly how the organizers' `fixtures.json` is loaded into them. It's for someone reviewing the design or writing a query; the [README](README.md) covers running the portal, and [ARCHITECTURE.md](ARCHITECTURE.md) the code around these tables.

**Contents**

* [Tables](#tables)
* [Rules the database enforces on its own](#rules-the-database-enforces-on-its-own)
* [How fixtures.json maps in](#how-fixturesjson-maps-in)
* [The awkward cases, and what we did](#the-awkward-cases-and-what-we-did)

`fixtures.json` (kept at `src/fixtures.json`, where the seeder and the Docker build can reach it) is input, not the schema. This is the schema, why it looks like this, and how the fixture lands in it. Code paths here are relative to `src/`.

## Tables

```
User 1---* EventRole *---1 Event 1---* Track
  |            (organizer | judge)  |  1---* Prize ---? Track
  |                                 |  1---* CustomQuestion
  |                                 |  1---* Activity
  1---* Membership *---1 Team *-----1 Event
                         |
                         1---* Project *---? Track
                                |  *---* Tag
                                |  1---* ProjectImage
                                |  1---* Answer *---1 CustomQuestion
                                +---? Project (duplicate_of)

Event 1---1 JudgingConfig        Event 1---* Criterion
Event 1---* JudgeInvite *---* Track
Event 1---* AssignmentBatch 1---* Assignment *---1 Project
                                     |   *---1 User (judge)
                                     1---1 Score 1---* CriterionScore *---1 Criterion
                                              1---* ScoreRevision (append-only)
User (judge) *---* Project through Conflict

Event 1---1 VotingConfig
Event 1---* Ballot 1---* BallotEntry *---1 Project
               *---? User (account and link ballots)
Event 1---* EmailPass (one-time links for email-gated voting)

Prize 1---* Award *---1 Project          Event 1---* Record *---? User (signed certificates and judges' records)
Project 1---* Comment *---1 User          Event 1---1 EmbedSettings
Event ?---* Webhook 1---* Delivery ---? Activity   (no event: a platform webhook)
User 1---* ApiToken                       User 1---* UserSession
LoginAttempt, Throttle                    (rate limiting; no links, keyed by address or person)
```

How to read it: `1---*` is one to many, `1---1` one to one, `*---*` many to many, and `?` marks a link that may be empty. Every table also has an integer `id`, and the timestamps (`created_at`, `updated_at`, `submitted_at` and so on) are stored in UTC.

### accounts

| Model | Key fields | Notes |
| --- | --- | --- |
| `User` | email (unique, stored lowercase), name, platform_role (`member`, `organizer`, `admin`), is_active, external_id | Email is the username. `external_id` records where a seeded account came from, e.g. `jdg_24`. |
| `UserSession` | user, session_key (unique), device, ip, created_at, last_seen_at | One row per signed-in browser, mirroring Django's session table so people can end sessions. |
| `LoginAttempt` | email, ip, succeeded, created_at | Feeds the login throttle. Rows older than a day are deleted. |

### events

| Model | Key fields | Notes |
| --- | --- | --- |
| `Event` | slug (unique), name, tagline, description, location, timezone, starts_at, submissions_close_at, judging_ends_at, results_at, max_team_size, is_published, comments_enabled, gallery_before_deadline | All datetimes are UTC. A check constraint keeps the deadline after kickoff. `gallery_before_deadline` (off by default) shows submitted projects publicly before the deadline; off, they wait for it. |
| `EventRole` | event, user, role (`organizer`, `judge`), tracks (M2M), all_tracks | Unique on (event, user, role). A judge's tracks decide what they may review; `all_tracks` makes them a floater who may review any track. |
| `Track` | event, name, description, position | Unique name per event. Its position picks one of eight print colours. |
| `Prize` | event, track (optional), name, value, quantity, position | No track means an overall prize. `value` is free text: "$1,500" or "Mechanical keyboard". |
| `Award` | prize, project, note, awarded_by, created_at | A prize given to a project. Unique per prize and project; no more awards than the prize's quantity; a track prize only to that track. Public once the event's results date has passed (or at once if it has none). |
| `CustomQuestion` | event, prompt, help_text, kind, options, required, is_public, position | Kinds: short answer, paragraph, link, pick one, yes or no. Private answers are shown only to the team and organizers. |
| `Activity` | event (empty for platform entries, including account changes: new accounts, password changes, API tokens), actor, verb, detail, team, project, created_at | Append-only log with server timestamps, including refused late edits (`late.refused`), rate-limit refusals (`rate.limited`, once per burst) and organizer anti-abuse decisions. |

### teams

| Model | Key fields | Notes |
| --- | --- | --- |
| `Team` | event, name, invite_code (unique), created_by, external_id | Names are deliberately not unique. The invite code is 128 random bits. |
| `Membership` | team, user, event, role (`captain`, `member`), joined_at | `event` is copied from the team so the database can enforce **one team per person per event** with a unique constraint on (event, user). |

### projects

| Model | Key fields | Notes |
| --- | --- | --- |
| `Project` | event, team, track, title, tagline, description, thumbnail, demo_video_url, repo_url, live_url, tags, status (`draft`, `submitted`), submitted_at, updated_at, last_edited_by, duplicate_of, external_id | A partial unique constraint on (event, team) where `duplicate_of` is null means **one listed project per team**, while still letting a flagged duplicate exist. |
| `ProjectImage` | project, path, width, height, position | Up to eight per project. Files live under `DATA_DIR/media/projects/<team id>/`. |
| `Answer` | project, question, value | Unique on (project, question). Blank answers are deleted rather than stored. |
| `Tag` | name (unique, lowercase) | Tags are normalised: trimmed, lowercased, inner spaces collapsed. |
| `Comment` | project, author, body (up to 2000 characters), created_at, removed_at, removed_by, removal_reason | Only listed projects get comments. Removal is soft, so an organizer's removal and its reason stay on record. |

### judging

| Model | Key fields | Notes |
| --- | --- | --- |
| `JudgingConfig` | event (one-to-one), scale_min, scale_max, reviews_per_project, kappa, results_published_at, results_published_by, share_feedback | Created on first use. Check constraints keep the scale a real range, at least one review per project and kappa non-negative. |
| `Criterion` | event, key, label, description, weight, position | The rubric. Unique key per event; weight must be above zero. Every criterion shares the event's scale. |
| `JudgeInvite` | event, token (unique), email, tracks, all_tracks, expires_at, accepted_by, accepted_at, revoked_at | Single-use link. If `email` is set, only that account can accept. |
| `AssignmentBatch` | event, label, mode (`algorithmic`, `import`), scope, seed, target_reviews, shortfall_count, shortfall_detail | One run of the engine, or one import. The seed makes a run reproducible. |
| `Assignment` | event, judge, project, batch (null for hand assignments), created_by, created_at | The gate for everything a judge may see. Unique on (judge, project). |
| `Conflict` | event, judge, project, reason, declared_by | The engine never assigns across one. Unique on (judge, project). |
| `Score` | assignment (one-to-one), event, judge, project, comment, submitted_at, source (`judge`, `import`), updated_at | Null `submitted_at` is a draft, which never counts. Unique on (judge, project): a resubmission updates the row. |
| `CriterionScore` | score, criterion, value | One mark per criterion per score. |
| `ScoreRevision` | event, judge, project, score, values_json, comment, submitted, source, actor, created_at | Append-only history: one row per save, never edited. |

`EventRole` also gained `all_tracks` (floater judges), and a judge's `tracks` now decide what they may review.

### api and webhooks

| Model | Key fields | Notes |
| --- | --- | --- |
| `ApiToken` | user, name, prefix, token_hash (unique), created_at, last_used_at, revoked_at | A personal access token. Only the SHA-256 of the token is stored; `prefix` identifies it on the account page. Revoked by the owner, by a password change, or unusable when the owner is deactivated. |
| `Webhook` | event (empty for platform webhooks), url, description, categories, secret, is_active, created_by, failure_streak, disabled_reason | `categories` are the audit log's own (`all`, `refused`, `submissions`, `judging`, ...). The secret signs deliveries. |
| `Delivery` | webhook, activity, uid (unique), kind, payload, status (`pending`, `succeeded`, `failed`), attempts, next_attempt_at, locked_until, last_status, last_error, last_duration_ms, response_excerpt, delivered_at | One send of one audit entry (or a test ping) to one webhook, with its retries. `locked_until` is how a sender claims it. |

### records

| Model | Key fields | Notes |
| --- | --- | --- |
| `Record` | event, user, kind (`participant`, `award`, `judge`, `organizer`), subject, code (unique), payload, signature, key_id, private, issued_at, issued_by, revoked_at, revoked_reason | A signed certificate or judge's record. `payload` is the exact JSON signed with Ed25519; `private` holds what a judge's record only commits to (their scores). One live record per person, kind and subject (`team:4`, `award:12`, `judge`), so issuing again adds nothing; a revoked one can be reissued. The code (16 characters in groups of four, 80 random bits) is printed on the certificate and is how anyone looks it up. |

### embeds

| Model | Key fields | Notes |
| --- | --- | --- |
| `EmbedSettings` | event (one-to-one), enabled, allowed_origins | Created when an organizer first saves the Embed tab; until then embedding is allowed anywhere. `allowed_origins` is one site per line (scheme and host only) and becomes the widget's `frame-ancestors`. |

### transfer

No tables. Imports create ordinary rows in the other apps, matched to people by email; the file formats are described in ARCHITECTURE.md (Import and export) and in the README.txt inside every archive.

### integrity

| Model | Key fields | Notes |
| --- | --- | --- |
| `Throttle` | scope, key, refused, created_at | One counted action for a rate limit with no table of its own (sign-ups per network, ballot and submission saves), or the marker that a key's refusal has been logged this window. Rows older than a day are swept. |

### voting

| Model | Key fields | Notes |
| --- | --- | --- |
| `VotingConfig` | event (one-to-one), is_enabled, access (`link`, `email`, `account`), method (`quadratic`, `single`), credits, max_votes, order (`shuffled`, `alphabetical`), opens_at, closes_at, link_token (unique), email_domains | Created when an organizer first opens the voting page. Empty `opens_at` means "when submissions close". `results_published_at` and `results_published_by` record the organizer's publication; results are public only when voting has closed and that is set. Access, method, credits, max_votes and order lock once a ballot exists. A voter's shuffled order isn't stored; it's recomputed from a seed. Check constraints keep credits and max_votes positive and the window in order. |
| `Ballot` | event, key (unique, names the ballot in the voter's session), kind, user, email, ip_hash, user_agent, created_at, updated_at | One per voter: partial unique constraints on (event, user) and (event, email). `ip_hash` is a salted SHA-256, never the raw address. `excluded_at`, `excluded_by` and `excluded_reason` record an organizer leaving it out of the count after review. |
| `BallotEntry` | ballot, project, votes | Unique on (ballot, project); votes at least 1 (a project with no votes has no row). Saving a ballot replaces its entries. |
| `EmailPass` | event, email, token_hash (unique), ip_hash, created_at, expires_at, used_at | A one-time link, valid 30 minutes. Only the hash of the token is stored. |

Nothing derived is stored: weighted totals, z-scores and rankings are computed from the marks on every request, and the community vote count from the ballot entries.

## Rules the database enforces on its own

Even if a view forgot a check, these would still hold:

* one team per person per event (`one_team_per_person_per_event`)
* one listed project per team (`one_listed_project_per_team`)
* one answer per question per project
* unique track names per event, unique invite codes, unique event slugs, unique emails
* deadline after kickoff (`event_deadline_after_kickoff`)
* a track that still has projects can't be deleted (`on_delete=RESTRICT`)
* one assignment, one score and one conflict row per judge and project
* a score can only exist for an assignment (one-to-one), so there is no way to hold a ballot for an unassigned project
* one mark per criterion per score; criterion weights above zero; a judging scale that is a real range
* one community ballot per account and per email address in an event, one line per project on a ballot, and at least one vote on every line

Rules that need the clock or a count (the deadline, team size, staff not competing) are enforced in the service layer, inside a transaction that holds SQLite's write lock.

## How fixtures.json maps in

| Fixture | Becomes | Notes |
| --- | --- | --- |
| `event.id`, `name`, `submissions_close` | `Event.external_id`, `name`, `submissions_close_at` | `starts_at` isn't in the fixture, so it's set 72 hours before the deadline. Slug `sample-hack-2026`, time zone UTC, published, team limit 4 (the largest fixture team). |
| `tracks[]` | `Track` | Order kept as `position`. |
| `judges[]` | `User` + `EventRole(judge)` + judge tracks | `external_id` keeps `jdg_xx`. |
| `teams[]` | `Team` + `Membership` | The first email in `members` becomes captain. Member accounts are created from the emails; display names come from the address (`priya1@` becomes "Priya", `member7_2@` becomes "Member 7-2"). |
| `projects[]` | `Project` | `summary` goes to `tagline`, `repo_url` as is, status submitted, `submitted_at` kept. The description is empty because the fixture has none. |
| `scores[].criteria` keys | `Criterion` | functionality, quality and innovation, weight 1 each (the fixture gives no weights), scale 1 to 5, with a short description for judges. |
| `scores[]` | `Assignment` + `Score` + `CriterionScore` + `ScoreRevision` | Each score becomes a completed assignment in one "Imported from fixtures.json" batch, submitted, source `import`. Comments kept. Marks outside 1 to 5 would be skipped and reported; there are none. |

Seeding matches on these external ids and only creates missing rows, so it's safe to run on every boot and never overwrites edits made in the portal. The fixture has no votes; in demo mode the seed adds a `VotingConfig` to each event (the fixture event's open and email-gated, the demo event's opening at its deadline and needing an account) and no ballots.

## The awkward cases, and what we did

| Case in the fixture | Handling |
| --- | --- |
| `prj_41` repeats `prj_07`: same team (`tm_07`), title, summary and repo, handed in about thirteen and a half hours later | Imported with `duplicate_of = prj_07`. It's hidden from the gallery and from counts, visible to organizers under "Needs a decision", and an organizer can swap which of the two is listed. We kept the earlier one listed because the two are identical and it's the one the judges saw first. Both carry scores in the fixture; the last row says what happens to them. |
| Two teams called StillTrail (`tm_30`, `tm_40`) | Team names aren't unique. The organizer's team list shows ids. |
| Thirteen single-person teams | Allowed. The team limit is a maximum, not a minimum. |
| A submission after the deadline | None in this fixture. If there were, it would be imported as a draft (so never listed) and reported in the seed output. |
| `jdg_07` gave every project a 4 | Imported as is. Normalization turns those scores into neutral votes (z = 0) rather than letting them pull projects toward the middle; the results page labels the judge. |
| Two judges (`jdg_01`, `jdg_23`) scored exactly one project | Also neutral: one score can't be told apart from severity. |
| Two review batches were never finished, so projects have two to five reviews | Imported as they are. In demo mode the seed then runs one "top up to three reviews" batch (seed 2026): eight new reviews, all within the judges' tracks, left pending. Results show "informative" review counts and flag projects ranked on fewer than two. |
| Four judges scored the duplicate `prj_41`, three of whom also scored `prj_07` | Kept in the record and the exports, but left out of the rankings while `prj_41` is flagged, so no judge's view of one piece of work counts twice. Listing `prj_41` instead swaps which set counts. |
