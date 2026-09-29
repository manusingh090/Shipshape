"""The API's own documentation, generated from the endpoint table."""

from django.shortcuts import render
from django.urls import reverse

from . import schema as sc
from .core import ENDPOINTS, endpoint

GROUPS = [
    ("Your account and tokens", lambda p: p.startswith(("auth/", "me"))),
    ("Admin", lambda p: p.startswith("admin/")),
    ("Webhooks", lambda p: "webhooks" in p or p.startswith("deliveries")),
    ("Prizes, certificates and records", lambda p: "records" in p or "/awards" in p),
    ("Anti-abuse", lambda p: "/integrity" in p),
    ("Community vote", lambda p: "/voting" in p or "/ballot" in p or "/vote/" in p),
    ("Judging", lambda p: "/judging" in p or p.startswith(("judge/", "judge-invites/")) or "/export/" in p),
    ("Teams", lambda p: "/team" in p and "/teams" not in p or p.startswith("invites/")),
    ("Projects, gallery and comments", lambda p: p.startswith("projects") or "/submission" in p),
    ("Events and the organizer console", lambda p: True),
]
METHOD_ORDER = {"GET": 0, "POST": 1, "PUT": 2, "PATCH": 3, "DELETE": 4}


def grouped():
    groups = {name: [] for name, _ in GROUPS}
    for e in ENDPOINTS:
        name = next(n for n, rule in GROUPS if rule(e.path))
        groups[name].append(e)
    return [(name, sorted(items, key=lambda e: (e.display_path, METHOD_ORDER.get(e.method, 9))))
            for name, items in groups.items() if items]


INDEX = sc.obj(
    name=sc.string(), version=sc.string(), reference=sc.url("The reference, as a page."),
    openapi=sc.url("The OpenAPI 3.1 document."), authentication=sc.string(),
    endpoints=sc.array(sc.obj(method=sc.string(), path=sc.string(), who=sc.string(), summary=sc.string(),
                              body=sc.nullable(sc.string()), same_as=sc.nullable(sc.string("The UI it matches."))))
)


@endpoint("GET", "", who="anyone", auth=False, summary="This index: every endpoint, what it does and who may call it.",
          returns=INDEX)
def index(request):
    return {
        "name": "Shipshape REST API", "version": "v1",
        "reference": request.build_absolute_uri(reverse("api_docs")),
        "openapi": request.build_absolute_uri("/api/v1/openapi.json"),
        "authentication": "Authorization: Bearer <token> (make one at /account/ or POST /api/v1/auth/tokens), "
                          "or the browser's session cookie for JSON requests from this site.",
        "endpoints": [{"method": e.method, "path": e.display_path, "who": e.who, "summary": e.summary,
                       "body": e.body or None, "same_as": e.ui or None} for _, items in grouped() for e in items],
    }


def shape(e):
    """A short name for what an endpoint returns, for the reference page."""
    if e.produces != "application/json":
        return {"text/csv": "CSV", "application/zip": "a .zip"}.get(e.produces, e.produces)
    r = e.returns or {}
    if "$ref" in r:
        return r["$ref"].rsplit("/", 1)[-1]
    items = (r.get("properties", {}).get("results") or {}).get("items", {})
    if "$ref" in items:
        return ("a page of " if r.get("x-shipshape-paginated") else "a list of ") + items["$ref"].rsplit("/", 1)[-1]
    return ""


def reference(request):
    groups = [(name, [{"method": e.method, "display_path": e.display_path, "summary": e.summary, "who": e.who,
                       "ui": e.ui, "body": e.body, "shape": shape(e)} for e in items]) for name, items in grouped()]
    return render(request, "api/reference.html", {"groups": groups, "count": len(ENDPOINTS),
                                                  "base": request.build_absolute_uri("/api/v1/")})
