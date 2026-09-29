"""The REST API's plumbing: authentication, the endpoint table, errors.

Every endpoint is declared with @endpoint(method, path, ...) and lands in
ENDPOINTS. The URL patterns, the JSON index at /api/v1/ and the HTML
reference at /api/v1/docs are all built from that one table, so the
documentation can't drift from what the router serves.

Authentication, either of:

* `Authorization: Bearer ss_...`, a personal access token made on the
  account page or with POST /api/v1/auth/tokens. Nothing is stored in a
  browser, so no CSRF defence is needed.
* The browser's session cookie. Then a write must be JSON (a cross-site form
  can't send that without a CORS preflight this app never grants), a foreign
  Origin is refused, and SameSite=Lax does the rest, exactly as for the older
  /api/ routes. Uploads (multipart) need a token, because a form on another
  site can send multipart.

Endpoints call the same services and validate with the same forms as the
pages, so the API can't be more permissive than the UI, or less.
"""

import json
import re
from dataclasses import dataclass
from functools import wraps
from urllib.parse import urlsplit

from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.http import Http404, JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from events.deadline import WindowError
from integrity import limits

from .models import ApiToken, hash_token

SAFE = {"GET", "HEAD", "OPTIONS"}


class ApiError(Exception):
    def __init__(self, status, code, detail, **extra):
        super().__init__(detail)
        self.status, self.code, self.detail, self.extra = status, code, detail, extra

    def response(self):
        return JsonResponse({"error": self.code, "detail": self.detail, **self.extra}, status=self.status)


@dataclass
class Endpoint:
    method: str
    path: str            # Django path syntax, below /api/v1/
    view: object
    who: str             # "anyone", "signed in", "organizer", "judge", "admin", ...
    summary: str
    ui: str = ""         # the page or form this does the same job as
    body: str = ""       # an example body, for people
    auth: bool = True
    upload: bool = False  # multipart only, and only with a token
    request: dict = None      # JSON Schema of the JSON body (api/schema.py)
    multipart: dict = None    # JSON Schema of the multipart/form-data fields
    returns: dict = None      # JSON Schema of a successful answer
    query: dict = None        # name -> JSON Schema of each query parameter
    produces: str = "application/json"
    created: bool = False     # may answer 201 Created (same schema) when it makes something

    @property
    def display_path(self):
        return "/api/v1/" + re.sub(r"<(?:\w+:)?(\w+)>", r"{\1}", self.path)


ENDPOINTS = []


def endpoint(method, path, *, who, summary, ui="", body="", upload=False, auth=True, request=None,
             multipart=None, returns=None, query=None, produces="application/json", created=False):
    """Register a view. auth=False lets anonymous callers in (the view still
    decides what they may see). request, multipart, returns and query are
    JSON Schemas for the OpenAPI document; returns is also checked against
    real answers during the test run."""
    def register(view):
        entry = Endpoint(method, path, None, who, summary, ui, body, auth, upload, request, multipart, returns,
                         query, produces, created)

        @wraps(view)
        def run(request, **kwargs):
            response = _handle(view, request, kwargs, method=method, upload=upload, auth=auth)
            if CONTRACT is not None:
                _check_contract(entry, response)
            return response
        entry.view = run
        ENDPOINTS.append(entry)
        return run
    return register


# ------------------------------------------------------- the contract ----
# Switched on by the test runner (portal/testing.py). Every answer an
# endpoint gives during the tests is checked against what the OpenAPI
# document promises: a success against its `returns` schema, a refusal
# against the Error schema. A mismatch fails the test that made the request.

CONTRACT = None   # None (off), or {(method, path): set of status codes seen}
CONTRACT_PROBLEMS = None   # record mode (API_CONTRACT=record): {where: problems} instead of failing


def _check_contract(entry, response):
    from .schema import ref, validate

    CONTRACT.setdefault((entry.method, entry.path), set()).add(response.status_code)
    ok = 200 <= response.status_code < 300
    content_type = response.get("Content-Type", "").split(";")[0]
    if ok and entry.produces != "application/json":
        problems = [] if content_type == entry.produces else [f"answered {content_type}, documented {entry.produces}"]
    elif content_type != "application/json":
        problems = [f"answered {content_type}, documented application/json"]
    else:
        schema = (entry.returns if ok else ref("Error"))
        if schema is None:
            problems = ["no response schema is declared"]
        else:
            problems = validate(schema, json.loads(response.content))
    if problems and CONTRACT_PROBLEMS is not None:  # record mode: collect them all, fail nothing
        CONTRACT_PROBLEMS.setdefault(f"{entry.method} {entry.display_path} {response.status_code}", problems[:6])
    elif problems:
        raise AssertionError(f"{entry.method} {entry.display_path} answered {response.status_code} outside its "
                             "documented shape:\n  " + "\n  ".join(problems[:12]))


# ------------------------------------------------------------------ auth ----

def authenticate(request):
    """Swap in the token's owner when a Bearer token is sent."""
    header = request.headers.get("Authorization", "")
    if not header:
        request.api_token = None
        return
    kind, _, raw = header.partition(" ")
    if kind.lower() != "bearer" or not raw.strip():
        raise ApiError(401, "bad_authorization", "Send Authorization: Bearer <token>.")
    token = ApiToken.objects.select_related("user").filter(token_hash=hash_token(raw.strip())).first()
    if token is None or not token.is_active:
        raise ApiError(401, "bad_token", "That token isn't valid. It may have been revoked.")
    now = timezone.now()
    if token.last_used_at is None or (now - token.last_used_at).total_seconds() > 60:
        ApiToken.objects.filter(pk=token.pk).update(last_used_at=now)
    request.user = token.user
    request.api_token = token


def _check_browser_write(request, upload):
    """Session-cookie writes get the same cross-site defence as the older API."""
    if request.api_token is not None or request.method in SAFE:
        return
    if upload:
        raise ApiError(401, "token_required", "Uploads need an API token (Authorization: Bearer ...).")
    # A form on another site can POST (even with an empty body), but can't
    # send PATCH, PUT or DELETE without a CORS preflight, which never passes.
    if request.method == "POST" and request.content_type != "application/json":
        raise ApiError(415, "unsupported_media_type", "Send a JSON body with Content-Type: application/json.")
    origin = request.headers.get("Origin")
    if origin:
        parts = urlsplit(origin)
        if parts.netloc != request.get_host() or parts.scheme != request.scheme:
            raise ApiError(403, "cross_origin", "Requests from other sites are not accepted.")


def json_body(request):
    if not request.body:
        return {}
    if request.content_type != "application/json":
        raise ApiError(415, "unsupported_media_type", "Send a JSON body with Content-Type: application/json.")
    try:
        body = json.loads(request.body)
    except (ValueError, UnicodeDecodeError):
        raise ApiError(400, "invalid_json", "The request body isn't valid JSON.")
    if not isinstance(body, dict):
        raise ApiError(400, "invalid_json", "Send a JSON object.")
    return body


def rule_errors():
    """Every exception a service raises when one of its rules says no. Each is
    written for the person who hit it, so the API passes the message on."""
    from accounts.services import AccountError
    from events.services import EventError
    from integrity.services import ReviewError
    from judging.errors import JudgingError
    from projects.comments import CommentError
    from projects.images import ImageError
    from projects.services import SubmissionError
    from teams.services import TeamError
    from voting.services import VoteError

    return (AccountError, EventError, ReviewError, JudgingError, CommentError, ImageError, SubmissionError,
            TeamError, VoteError)


def _code(exc):
    return re.sub(r"(?<!^)(?=[A-Z])", "_", type(exc).__name__).lower().removesuffix("_error") or "refused"


def _handle(view, request, kwargs, *, method, upload, auth):
    try:
        if request.method != method:
            raise ApiError(405, "method_not_allowed", f"Use {method}.")
        authenticate(request)
        if auth and not request.user.is_authenticated:
            raise ApiError(401, "not_signed_in", "Send a session cookie or an API token.")
        _check_browser_write(request, upload)
        if request.method not in SAFE and request.user.is_authenticated:
            limits.check("api.write", request.user.pk, "That's a lot of API writes in a minute. Slow down.",
                         actor=request.user)
            limits.record("api.write", request.user.pk)
        response = view(request, **kwargs)
    except ApiError as err:
        response = err.response()
    except WindowError as err:
        response = ApiError(403, err.code, err.message).response()
    except limits.Limited as err:
        response = ApiError(429, err.code, err.message).response()
    except PermissionDenied as err:
        response = ApiError(403, "forbidden", str(err) or "You can't do that.").response()
    except Http404:
        response = ApiError(404, "not_found", "Nothing here, or nothing you can see.").response()
    except rule_errors() as err:
        message = getattr(err, "message", None) or str(err)
        extra = {"missing": err.problems} if getattr(err, "problems", None) else {}
        response = ApiError(getattr(err, "status", 400), getattr(err, "code", _code(err)), message,
                            **extra).response()
    if isinstance(response, (dict, list)):
        response = JsonResponse(response, safe=False)
    response["Cache-Control"] = "no-store"
    return response


def dispatcher(path):
    """One Django view per path, choosing the endpoint by HTTP method."""
    by_method = {e.method: e.view for e in ENDPOINTS if e.path == path}

    @csrf_exempt
    def view(request, **kwargs):
        target = by_method.get(request.method)
        if target is None:
            allowed = ", ".join(sorted(by_method))
            response = ApiError(405, "method_not_allowed", f"Use {allowed}.").response()
            response["Allow"] = allowed
            return response
        return target(request, **kwargs)
    return view


# --------------------------------------------------------------- helpers ----

def form_errors(form):
    fields = {name: [e["message"] for e in errors] for name, errors in form.errors.get_json_data().items()}
    return ApiError(400, "invalid", "Some fields need fixing.", fields=fields)


def valid(form):
    if not form.is_valid():
        raise form_errors(form)
    return form


def paginate(request, queryset, serialize, per_page=50):
    try:
        size = max(1, min(200, int(request.GET.get("per_page", per_page))))
    except ValueError:
        size = per_page
    page = Paginator(queryset, size).get_page(request.GET.get("page"))
    return {"count": page.paginator.count, "page": page.number, "pages": page.paginator.num_pages,
            "results": [serialize(item) for item in page.object_list]}


def ids(values):
    out = []
    for value in values or []:
        try:
            out.append(int(value))
        except (TypeError, ValueError):
            raise ApiError(400, "invalid", f"{value!r} isn't an id.")
    return out
