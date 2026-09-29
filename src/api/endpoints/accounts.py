"""Accounts: tokens, sign-up, your profile, password and sessions; admins."""

from django.contrib.auth import authenticate
from django.contrib.auth.forms import PasswordChangeForm
from django.utils import timezone

from accounts import services, throttle
from accounts.forms import ProfileForm, SignupForm
from accounts.models import User, UserSession, normalize_email
from events import audit
from events.models import Activity, log_activity
from integrity import limits

from .. import schema as sc
from .. import serializers as s
from ..core import ApiError, endpoint, json_body, paginate, valid
from ..models import ApiToken


# What each endpoint takes and returns (api/schema.py), for the OpenAPI document.
TOKEN_BODY = sc.obj(name=sc.optional(sc.string("A label, to recognise the token later.")),
                    email=sc.optional(sc.string("Signed out: your account's address.")),
                    password=sc.optional(sc.string("Signed out: your password.")))
NEW_TOKEN = sc.obj(token=sc.string("The token itself. It's shown this once and never stored."), note=sc.string(),
                   **sc.COMPONENTS["Token"]["properties"])
SIGNUP_BODY = sc.obj(name=sc.string(), email=sc.string(format="email"),
                     password=sc.string("At least 8 characters, not too common, not all numbers."),
                     token_name=sc.optional(sc.string("A label for the token made with the account.")))
ME = sc.obj(**sc.COMPONENTS["UserAdmin"]["properties"],
            staff=sc.array(sc.obj(event=sc.string(), role=sc.enum("organizer", "judge")), "Events you help run or judge."),
            teams=sc.array(sc.obj(event=sc.string(), team=sc.integer(), role=sc.enum("captain", "member"))))
SESSION = sc.obj(id=sc.integer(), device=sc.string("The browser, in words."), ip=sc.nullable(sc.string()),
                 created_at=sc.when(), last_seen_at=sc.nullable(sc.when()),
                 current=sc.boolean("The session this request came from."))
ADMIN_UPDATE = sc.obj(platform_role=sc.optional(sc.enum("member", "organizer", "admin")),
                      is_active=sc.optional(sc.boolean()))
AUDIT_QUERY = {"only": sc.string("One kind of entry, as on the audit page (refused, integrity, account...)."),
               "q": sc.string("Search names, emails and text.")}


def _admin(request):
    if not request.user.is_admin:
        raise ApiError(403, "forbidden", "Platform admins only.")


@endpoint("POST", "auth/tokens", who="anyone with a password, or signed in", auth=False,
          summary="Make a personal access token. Signed out, send email and password (sign-in limits apply).",
          ui="Account page, API tokens", body='{"name": "my script", "email": "...", "password": "..."}',
          request=TOKEN_BODY, returns=NEW_TOKEN)
def create_token(request):
    body = json_body(request)
    user = request.user if request.user.is_authenticated else None
    if user is None:
        email = normalize_email(body.get("email", ""))
        ip = throttle.client_ip(request)
        if throttle.lockout_remaining(email, ip):
            limits.note_refusal("login.address", email, detail=f"token sign-in for {email} locked")
            raise ApiError(429, "slow_down", "Too many wrong passwords for this account. Try again later.")
        user = authenticate(request, username=email, password=str(body.get("password", "")))
        throttle.record(email, ip, succeeded=user is not None)
        if user is None:
            raise ApiError(401, "bad_credentials", "That email and password don't match an account we know.")
    token, raw = ApiToken.issue(user, body.get("name", ""))
    log_activity(None, user, "account.token", f"made an API token “{token.name}” ({token.prefix}…)")
    return {"token": raw, "note": "Shown once. Send it as Authorization: Bearer <token>.", **s.token(token)}


@endpoint("GET", "auth/tokens", who="signed in", summary="Your API tokens (never the tokens themselves).",
          ui="Account page, API tokens",
          returns=sc.results(sc.ref("Token")))
def list_tokens(request):
    return {"results": [s.token(t) for t in ApiToken.objects.filter(user=request.user)]}


@endpoint("DELETE", "auth/tokens/<int:pk>", who="signed in", summary="Revoke one of your tokens.",
          ui="Account page, Revoke",
          returns=sc.ref("Token"))
def revoke_token(request, pk):
    token = ApiToken.objects.filter(user=request.user, pk=pk, revoked_at__isnull=True).first()
    if token is None:
        raise ApiError(404, "not_found", "No such token, or it's already revoked.")
    token.revoked_at = timezone.now()
    token.save(update_fields=["revoked_at"])
    log_activity(None, request.user, "account.token", f"revoked the API token “{token.name}” ({token.prefix}…)")
    return s.token(token)


@endpoint("POST", "auth/signup", who="anyone", auth=False, summary="Make an account and get a token for it.",
          ui="Sign up page", body='{"name": "...", "email": "...", "password": "..."}',
          request=SIGNUP_BODY, returns=sc.obj(user=sc.ref("PersonWithEmail"), token=sc.string()))
def signup(request):
    body = json_body(request)
    password = str(body.get("password", ""))
    form = valid(SignupForm({"name": body.get("name", ""), "email": body.get("email", ""),
                             "password1": password, "password2": password}))
    user = services.sign_up(form, limits.network_key(throttle.client_ip(request)))
    token, raw = ApiToken.issue(user, body.get("token_name") or "made at sign-up")
    return {"user": s.person(user, email=True), "token": raw}


@endpoint("GET", "me", who="signed in", summary="Who you are, and your roles in each event.", ui="Account page",
          returns=ME)
def me(request):
    from events.models import EventRole
    from teams.models import Membership

    u = request.user
    return {**s.user_admin(u),
            "staff": [{"event": r.event.slug, "role": r.role} for r in EventRole.objects.filter(user=u).select_related("event")],
            "teams": [{"event": m.event.slug, "team": m.team_id, "role": m.role}
                      for m in Membership.objects.filter(user=u).select_related("event")]}


@endpoint("PATCH", "me", who="signed in", summary="Change your name.", ui="Account page, Your details",
          body='{"name": "..."}',
          request=sc.obj(name=sc.string(maxLength=120)), returns=sc.ref("UserAdmin"))
def update_me(request):
    body = json_body(request)
    form = valid(ProfileForm({"name": body.get("name", request.user.name)}, instance=request.user))
    services.update_profile(request.user, form)
    return s.user_admin(request.user)


@endpoint("POST", "me/password", who="signed in",
          summary="Change your password. Ends every other session and revokes your API tokens.",
          ui="Account page, Change password", body='{"old_password": "...", "new_password": "..."}',
          request=sc.obj(old_password=sc.string(), new_password=sc.string()),
          returns=sc.obj(changed=sc.boolean(), sessions_ended=sc.integer(), note=sc.string()))
def change_password(request):
    body = json_body(request)
    new = str(body.get("new_password", ""))
    form = valid(PasswordChangeForm(request.user, {"old_password": body.get("old_password", ""),
                                                   "new_password1": new, "new_password2": new}))
    keep = request.session.session_key if request.api_token is None else None
    ended = services.change_password(request.user, form, keep_key=keep)
    return {"changed": True, "sessions_ended": ended,
            "note": "Your API tokens were revoked with the old password; make a new one."}


@endpoint("GET", "me/sessions", who="signed in", summary="Browsers you're signed in on.", ui="Account page, Sessions",
          returns=sc.results(SESSION))
def sessions(request):
    current = request.session.session_key
    return {"results": [{"id": r.pk, "device": r.device, "ip": r.ip, "created_at": s.when(r.created_at),
                         "last_seen_at": s.when(r.last_seen_at), "current": r.session_key == current}
                        for r in UserSession.objects.filter(user=request.user)]}


@endpoint("DELETE", "me/sessions/<int:pk>", who="signed in", summary="Sign one browser out.",
          ui="Account page, Sign out (per session)",
          returns=sc.obj(ended=sc.boolean()))
def end_session(request, pk):
    row = UserSession.objects.filter(user=request.user, pk=pk).first()
    if row is None:
        raise ApiError(404, "not_found", "No such session.")
    services.end_one_session(request.user, row, current_key=request.session.session_key)
    return {"ended": True}


@endpoint("POST", "me/sessions/end-others", who="signed in", summary="Sign out every other browser.",
          ui="Account page, Sign out everywhere else",
          returns=sc.obj(ended=sc.integer("How many sessions were ended.")))
def end_others(request):
    keep = request.session.session_key if request.api_token is None else None
    return {"ended": services.end_other_sessions(request.user, keep_key=keep)}


@endpoint("GET", "admin/users", who="admin", summary="Every account; ?q= searches, ?role= filters.",
          ui="Admin page",
          returns=sc.page(sc.ref("UserAdmin")),
          query={"q": sc.string("Search names and emails."),
                 "role": sc.enum("member", "organizer", "admin", "inactive", description="Only these.")})
def admin_users(request):
    _admin(request)
    users = User.objects.all().order_by("name", "email")
    q, role = request.GET.get("q", "").strip(), request.GET.get("role", "")
    if q:
        from django.db.models import Q
        users = users.filter(Q(email__icontains=q) | Q(name__icontains=q))
    if role in User.Role.values:
        users = users.filter(platform_role=role)
    elif role == "inactive":
        users = users.filter(is_active=False)
    return paginate(request, users, s.user_admin)


@endpoint("PATCH", "admin/users/<int:pk>", who="admin", summary="Change someone's platform role or deactivate them.",
          ui="Admin page, Save (per person)", body='{"platform_role": "organizer", "is_active": true}',
          request=ADMIN_UPDATE, returns=sc.ref("UserAdmin"))
def admin_update_user(request, pk):
    _admin(request)
    target = User.objects.filter(pk=pk).first()
    if target is None:
        raise ApiError(404, "not_found", "No such account.")
    body = json_body(request)
    active = body.get("is_active", target.is_active)
    if not isinstance(active, bool):
        raise ApiError(400, "invalid", '"is_active" is true or false.')
    target, _ = services.admin_update(request.user, target, body.get("platform_role", target.platform_role), active)
    return s.user_admin(target)


@endpoint("GET", "admin/audit", who="admin", summary="The platform audit log; ?only= and ?q= filter it.",
          ui="Admin, Platform audit log",
          query=AUDIT_QUERY, returns=sc.page(sc.ref("Activity")))
def admin_audit(request):
    _admin(request)
    entries, _, _ = audit.filtered(Activity.objects.filter(event__isnull=True).select_related("actor"),
                                   request.GET.get("only", ""), request.GET.get("q", ""))
    return paginate(request, entries, s.activity)
