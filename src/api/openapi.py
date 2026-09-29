"""The OpenAPI 3.1 document for the REST API, built from the endpoint table.

Nothing here is written by hand twice: paths, methods, who may call each
operation, what it's the same as in the UI, and the request and response
schemas all come from the @endpoint declarations (api/core.py) and the
shapes in api/schema.py. It's served at /api/v1/openapi.json, and a copy is
kept in src/api/openapi.json (`manage.py openapi` writes it; a test fails if
it's stale), so it can be read without running anything.
"""

import re
from dataclasses import replace

from . import schema as sc
from .core import ENDPOINTS, endpoint

VERSION = "1.0.0"

DESCRIPTION = """\
Everything the Shipshape portal's pages can do, as JSON. Every page and every form operation in the UI has an
endpoint here (a test walks the site's routes and templates to keep it that way), and each one calls the same
code and validates with the same forms as the page, so the API can't do more than the UI, or less.

**Authentication.** Either a personal access token, `Authorization: Bearer ss_...` (make one on the account page,
or with `POST /auth/tokens` and a password), or the browser's session cookie. With the cookie, a write must be
JSON and come from this site (a foreign `Origin` is refused), which is the cross-site defence; uploads
(multipart) need a token. Operations that say "anyone" work signed out, and show more to people who may see more.

**Errors.** Every refusal is JSON with a stable `error` code for programs and a `detail` written for people, for
example `403 {"error": "submissions_closed", "detail": "Submissions for ... closed at ..."}`. Validation errors add
`fields`, per field. Hidden things answer 404, exactly like things that don't exist.

**Time.** Moments are ISO 8601 in UTC. Dates you send may carry an offset, or be the event's wall-clock time
(`2026-03-01T18:00`), which is how the event's own forms read them. Every deadline is judged by the server's clock,
and the deadline instant itself counts as closed.

**Lists.** Long lists are paged: `?page=` (from 1) and `?per_page=` (at most 200), answered with `count`, `page`,
`pages` and `results`.

**Limits.** Writes are limited to 120 a minute per person, on top of the portal's own limits (sign-ins, ballots,
comments...); a refusal is `429 slow_down`.

**Older routes.** A few routes under `/api/` predate this version and are kept because the DOGFOOD acceptance
checker calls them. They're listed under "Older /api/ routes", each naming its `/api/v1/` twin.
"""

GROUPS = [
    ("Your account and tokens", lambda p: p.startswith(("auth/", "me")), "Sign-in tokens, your profile, sessions."),
    ("Admin", lambda p: p.startswith("admin/"), "Platform administration."),
    ("Webhooks", lambda p: "webhooks" in p or p.startswith("deliveries"), "Signed notifications of what happens."),
    ("Prizes, certificates and records", lambda p: "records" in p or "/awards" in p, "Awards and signed records."),
    ("Anti-abuse", lambda p: "/integrity" in p, "What the checks flagged, and organizers' decisions about it."),
    ("Community vote", lambda p: "/voting" in p or "/ballot" in p or "/vote/" in p, "Ballots, settings, results."),
    ("Judging", lambda p: "/judging" in p or p.startswith(("judge/", "judge-invites/")) or "/export/" in p and
     p.endswith(".csv"), "Judges, the rubric, assignments, scores, results and exports."),
    ("Import and export", lambda p: "/export/" in p or "import" in p, "The whole event in or out."),
    ("Teams", lambda p: "/team" in p and "/teams" not in p or p.startswith("invites/"), "Your team."),
    ("Projects, gallery and comments", lambda p: p.startswith("projects") or "/submission" in p,
     "The public gallery, your submission, comments."),
    ("Embedding", lambda p: "/embed" in p, "The embeddable gallery's settings."),
    ("Events and the organizer console", lambda p: True, "Events, and everything on an organizer's console."),
]
LEGACY_TAG = "Older /api/ routes"

# The routes under /api/ that came before /api/v1/, each with the v1 operation
# that does the same job. Where they share a view, they share its schemas;
# the three that have their own view say what they take and return.
OLD_PROJECTS = sc.obj(count=sc.integer(), limit=sc.integer(), offset=sc.integer(),
                      results=sc.array(sc.ref("ProjectSummary")))
OLD_ASSIGNMENTS = sc.obj(count=sc.integer(), assignments=sc.array(sc.obj(
    event=sc.string(), project_id=sc.integer(), title=sc.string(), track=sc.nullable(sc.string()),
    status=sc.enum("pending", "draft", "submitted"))))
OLD_SCORE_BODY = sc.obj(project=sc.integer("The project's id."),
                        criteria=sc.mapping(sc.integer(), "Marks by criterion key."),
                        comment=sc.optional(sc.string()), submit=sc.optional(sc.boolean()))
OVERRIDES = {
    ("GET", "projects"): {"returns": OLD_PROJECTS, "query": {
        "event": sc.string("An event's slug."), "track": sc.integer("A track id."),
        "limit": sc.integer("1 to 200, default 50.", minimum=1, maximum=200), "offset": sc.integer(minimum=0)}},
    ("GET", "judge/assignments"): {"returns": OLD_ASSIGNMENTS, "query": {"event": sc.string("Only this event.")}},
    ("POST", "events/<slug:slug>/judging/scores"): {"returns": sc.ref("Score"), "request": OLD_SCORE_BODY},
}
LEGACY = [
    ("GET", "projects", "GET", "projects", "The gallery as JSON (the checker's gallery probe uses the page instead)."),
    ("GET", "events/<slug:slug>/submission", "GET", "events/<slug:slug>/submission", "Your team's submission."),
    ("POST", "events/<slug:slug>/submission", "POST", "events/<slug:slug>/submission",
     "Save or submit your team's project. The checker's late-submission probe calls this and expects 403."),
    ("GET", "judge/scores", "GET", "judge/scores", "Your own scores; the checker's isolation probes call this."),
    ("GET", "events/<slug:slug>/judging/progress", "GET", "events/<slug:slug>/judging/progress", "Judging progress."),
    ("GET", "events/<slug:slug>/judging/results", "GET", "events/<slug:slug>/judging/results", "Rankings."),
    ("GET", "events/<slug:slug>/export/<str:stage>.csv", "GET", "events/<slug:slug>/export/<str:stage>.csv",
     "A CSV export; the checker downloads final-rankings.csv."),
    ("GET", "events/<slug:slug>/ballot", "GET", "events/<slug:slug>/ballot", "Your ballot."),
    ("POST", "events/<slug:slug>/ballot", "POST", "events/<slug:slug>/ballot", "Save your ballot."),
    ("GET", "events/<slug:slug>/vote/results", "GET", "events/<slug:slug>/vote/results", "The community count."),
    ("GET", "judge/assignments", "GET", "judge/events/<slug:slug>/queue",
     "Your assignments in every event, in one list."),
    ("POST", "events/<slug:slug>/judging/scores", "PUT", "judge/events/<slug:slug>/projects/<int:pk>/score",
     "Save a score; the project id goes in the body."),
]

CONVERTERS = {
    "slug": {"type": "string", "pattern": "^[-a-zA-Z0-9_]+$"},
    "int": {"type": "integer", "minimum": 0},
    "str": {"type": "string"},
    "path": {"type": "string"},
}
PARAM_TEXT = {"slug": "The event's address, e.g. sample-hack-2026.", "pk": "The object's id.",
              "code": "The record's code.", "stage": "Which export: teams, projects, judges, assignments, raw-scores, "
              "normalized-scores, final-rankings, score-history, community-votes, ballots, comments or audit-log.",
              "token": "The token from the link."}
WRITES = {"POST", "PUT", "PATCH", "DELETE"}


def openapi_path(path, legacy=False):
    """Paths are relative to the server, /api: /v1/... for this version,
    and the older routes straight under it."""
    return ("/" if legacy else "/v1/") + re.sub(r"<(?:\w+:)?(\w+)>", r"{\1}", path)


def path_params(path):
    params = []
    for converter, name in re.findall(r"<(?:(\w+):)?(\w+)>", path):
        params.append({"name": name, "in": "path", "required": True, "description": PARAM_TEXT.get(name, ""),
                       "schema": CONVERTERS.get(converter or "str", CONVERTERS["str"])})
    return params


def operation_id(method, path):
    words = re.sub(r"<(?:\w+:)?(\w+)>", r"by_\1", path).replace(".", "_")
    return method.lower() + "_" + re.sub(r"[^a-z0-9]+", "_", words.lower()).strip("_") if path else "get_index"


def tag_for(path):
    return next(name for name, rule, _ in GROUPS if rule(path))


def _responses(e):
    ok = {"description": "Done." if e.method in WRITES else "What you asked for."}
    if e.produces == "application/json":
        ok["content"] = {"application/json": {"schema": e.returns or {}}}
    else:
        ok["content"] = {e.produces: {"schema": {"type": "string", "format": "binary"}
                                      if e.produces != "text/csv" else {"type": "string"}}}
    out = {"200": ok}
    if e.created:
        out["201"] = {**ok, "description": "Created."}
    if e.method in WRITES or e.query:
        out["400"] = {"$ref": "#/components/responses/Invalid"}
    out["401"] = {"$ref": "#/components/responses/SignIn"}
    if e.auth or e.method in WRITES:
        out["403"] = {"$ref": "#/components/responses/Forbidden"}
    if "<" in e.path:
        out["404"] = {"$ref": "#/components/responses/NotFound"}
    if e.method in WRITES:
        out["415"] = {"$ref": "#/components/responses/NotJson"}
        out["429"] = {"$ref": "#/components/responses/SlowDown"}
    return out


def _operation(e, path=None, legacy_note=None):
    path = e.path if path is None else path
    description = [e.summary, "", f"**Who:** {e.who}."]
    if e.ui:
        description.append(f"**Same as, in the UI:** {e.ui}.")
    if e.body:
        description.append(f"**Example body:** `{e.body}`")
    if legacy_note:
        description = [legacy_note, "", f"The same as `{e.method} /api{openapi_path(e.path)}`; "
                       "see there for the details."]
    op = {
        "operationId": operation_id(e.method, path) if not legacy_note else "legacy_" + operation_id(e.method, path),
        "summary": e.summary.split(". ")[0].rstrip(".")[:120],
        "description": "\n".join(description),
        "tags": [LEGACY_TAG if legacy_note else tag_for(e.path)],
        "responses": _responses(e),
        "x-shipshape-who": e.who,
    }
    if e.ui:
        op["x-shipshape-ui"] = e.ui
    params = path_params(path)
    for name, schema in (e.query or {}).items():
        params.append({"name": name, "in": "query", "required": False,
                       "description": schema.get("description", ""), "schema": schema})
    if (e.returns or {}).get("x-shipshape-paginated"):
        params += [{"$ref": "#/components/parameters/page"}, {"$ref": "#/components/parameters/per_page"}]
    if params:
        op["parameters"] = params
    content = {}
    if e.request is not None:
        content["application/json"] = {"schema": sc.resolve(e.request)}
    if e.multipart is not None:
        content["multipart/form-data"] = {"schema": sc.resolve(e.multipart)}
    if content:
        op["requestBody"] = {"required": e.method != "DELETE", "content": content}
    op["security"] = ([{"token": []}, {"session": []}] if e.auth
                      else [{}, {"token": []}, {"session": []}])
    return op


def document():
    paths = {}
    for e in ENDPOINTS:
        paths.setdefault(openapi_path(e.path), {})[e.method.lower()] = _operation(e)
    by_key = {(e.method, e.path): e for e in ENDPOINTS}
    for method, path, twin_method, twin_path, note in LEGACY:
        twin = by_key[(twin_method, twin_path)]
        own = OVERRIDES.get((method, path))
        if own:  # its own view: same job, its own shapes
            twin = replace(twin, returns=own["returns"], request=own.get("request"), query=own.get("query"))
        op = _operation(twin, path=path, legacy_note=note)
        # Older routes read the browser session only; a Bearer token is ignored there.
        op["security"] = [{"session": []}] if twin.auth else [{}, {"session": []}]
        op["x-shipshape-same-as"] = operation_id(twin_method, twin_path)
        paths.setdefault(openapi_path(path, legacy=True), {})[method.lower()] = op
    return {
        "openapi": "3.1.0",
        "info": {"title": "Shipshape REST API", "version": VERSION, "description": DESCRIPTION,
                 "license": {"name": "MIT", "identifier": "MIT"}},
        "servers": [{"url": "/api", "description": "This portal. Version 1 is under /v1; the older routes sit beside it."}],
        "tags": [{"name": name, "description": text} for name, _, text in GROUPS]
        + [{"name": LEGACY_TAG, "description": "Routes from before /api/v1/, kept for the acceptance checker."}],
        "paths": dict(sorted(paths.items())),
        "webhooks": {"activity": {"post": WEBHOOK_DELIVERY}},
        "components": {
            "schemas": dict(sorted(sc.COMPONENTS.items())),
            "parameters": {
                "page": {"name": "page", "in": "query", "required": False, "description": "Which page, from 1.",
                         "schema": {"type": "integer", "minimum": 1}},
                "per_page": {"name": "per_page", "in": "query", "required": False,
                             "description": "How many per page (default 50, at most 200).",
                             "schema": {"type": "integer", "minimum": 1, "maximum": 200}},
            },
            "responses": {
                "Invalid": _error("Something in the request needs fixing; `fields` says what, per field."),
                "SignIn": _error("Sign in first, or the token is wrong or revoked (not_signed_in, bad_token)."),
                "Forbidden": _error("Not allowed: not your event, not your role, outside the window "
                                    "(submissions_closed, judging_closed...), or a request from another site."),
                "NotFound": _error("Nothing here, or nothing you can see. The two are answered the same way."),
                "NotJson": _error("Writes with the session cookie must be JSON."),
                "SlowDown": _error("A rate limit: wait a moment (slow_down)."),
            },
            "securitySchemes": {
                "token": {"type": "http", "scheme": "bearer", "bearerFormat": "ss_...",
                          "description": "A personal access token from the account page or POST /auth/tokens."},
                "session": {"type": "apiKey", "in": "cookie", "name": "session",
                            "description": "The browser's session. Writes must be JSON from this site."},
            },
        },
    }


def _header(text, **schema):
    return {"description": text, "required": True, "schema": {"type": "string", **schema}}


# What the portal sends to a webhook's URL (set one up with POST /v1/events/{slug}/webhooks).
WEBHOOK_DELIVERY = {
    "operationId": "webhook_activity",
    "summary": "Something happened that the webhook listens for",
    "description": "One POST per audit-log entry the webhook's categories cover, sent once the change is saved "
                   "(a rolled-back action is never announced). A 2xx answer within 5 seconds is a success; "
                   "anything else is retried after 1 minute, 5 minutes, 30 minutes, 2 hours and 12 hours. Five "
                   "given-up deliveries in a row switch the webhook off. To check a delivery: HMAC-SHA256 the "
                   "string `<t>.<body>` with the webhook's secret, compare with `v1` in constant time, and refuse "
                   "a `t` more than five minutes old.",
    "tags": ["Webhooks"],
    "parameters": [
        {"name": "Shipshape-Signature", "in": "header", **_header("t=<unix seconds>,v1=<hex HMAC-SHA256>")},
        {"name": "Shipshape-Delivery", "in": "header", **_header("The delivery's id; the same on a retry.",
                                                                  format="uuid")},
        {"name": "Shipshape-Event", "in": "header", **_header("The payload's type, e.g. project.submitted.")},
    ],
    "requestBody": {"required": True, "content": {"application/json": {"schema": sc.ref("WebhookPayload")}}},
    "responses": {"2XX": {"description": "Received."},
                  "default": {"description": "Anything else, or no answer in 5 seconds, is retried."}},
}


def _error(text):
    return {"description": text, "content": {"application/json": {"schema": sc.ref("Error")}}}


@endpoint("GET", "openapi.json", who="anyone", auth=False,
          summary="This API's OpenAPI 3.1 document: every operation, with its request and response schemas.",
          returns=sc.obj("An OpenAPI 3.1 document.", _open=True, openapi=sc.string(), info=sc.obj(_open=True),
                         paths=sc.obj(_open=True), components=sc.obj(_open=True)))
def openapi_json(request):
    return document()
