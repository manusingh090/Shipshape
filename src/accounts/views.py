from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout, update_session_auth_hash
from django.contrib.auth.forms import PasswordChangeForm
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.http import Http404, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from api.models import ApiToken
from events import audit
from events.models import Activity, Event, EventRole, log_activity
from integrity import limits
from projects.models import Project
from teams.models import Membership

from . import services, throttle
from .decorators import admin_required, login_required
from .demo import demo_accounts, demo_user
from .forms import LoginForm, ProfileForm, SignupForm
from .models import User, UserSession, normalize_email


def safe_next(request, fallback=""):
    target = request.POST.get("next") or request.GET.get("next") or ""
    if target and url_has_allowed_host_and_scheme(
        target, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return target
    return fallback


def login_view(request):
    next_url = safe_next(request)
    if request.user.is_authenticated:
        return redirect(next_url or "home")

    form = LoginForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        email = normalize_email(form.cleaned_data["email"])
        ip = throttle.client_ip(request)
        wait = throttle.lockout_remaining(email, ip)
        if wait:
            limits.note_refusal("login.address", email, detail=f"sign-in for {email} locked")
            minutes = max(1, round(wait.total_seconds() / 60))
            form.add_error(
                None,
                f"Too many wrong passwords for this account. Try again in about {minutes} minute"
                f"{'s' if minutes != 1 else ''}.",
            )
        else:
            user = authenticate(request, username=email, password=form.cleaned_data["password"])
            throttle.record(email, ip, succeeded=user is not None)
            if user is not None:
                login(request, user)
                return redirect(next_url or "home")
            form.add_error(None, "That email and password don't match an account we know.")

    return render(request, "accounts/login.html", {
        "form": form,
        "next": next_url,
        "demo_accounts": demo_accounts() if settings.DEMO_MODE else [],
    })


@require_POST
def demo_login(request):
    if not settings.DEMO_MODE:
        raise Http404()
    user = demo_user(request.POST.get("who", ""))
    if user is None:
        raise Http404()
    if request.user.is_authenticated:
        logout(request)
    login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    messages.success(request, f"Signed in as {user.display_name}.")
    return redirect(safe_next(request) or "home")


@require_POST
def logout_view(request):
    logout(request)
    messages.info(request, "You're signed out.")
    return redirect("home")


def signup_view(request):
    next_url = safe_next(request)
    if request.user.is_authenticated:
        return redirect(next_url or "home")
    form = SignupForm(request.POST or None)
    network = limits.network_key(throttle.client_ip(request))
    if request.method == "POST" and form.is_valid():
        try:
            user = services.sign_up(form, network)
        except limits.Limited as err:
            form.add_error(None, err.message)
            return render(request, "accounts/signup.html", {"form": form, "next": next_url}, status=429)
        login(request, user, backend="django.contrib.auth.backends.ModelBackend")
        messages.success(request, f"Welcome aboard, {user.display_name}.")
        return redirect(next_url or "home")
    return render(request, "accounts/signup.html", {"form": form, "next": next_url})


def _account_context(request, profile_form=None, password_form=None):
    user = request.user
    staff_roles = (
        EventRole.objects.filter(user=user).select_related("event").order_by("-event__submissions_close_at")
    )
    memberships = (
        Membership.objects.filter(user=user).select_related("team", "event").order_by("-event__submissions_close_at")
    )
    return {
        "profile_form": profile_form or ProfileForm(instance=user),
        "password_form": password_form or PasswordChangeForm(user),
        "sessions": UserSession.objects.filter(user=user),
        "tokens": ApiToken.objects.filter(user=user, revoked_at__isnull=True),
        "records": user.records.select_related("event").order_by("-issued_at"),
        "new_token": request.session.pop("new_api_token", None),
        "current_key": request.session.session_key,
        "staff_roles": staff_roles,
        "memberships": memberships,
    }


@login_required
def account_view(request):
    profile_form = None
    if request.method == "POST":
        profile_form = ProfileForm(request.POST, instance=request.user)
        if profile_form.is_valid():
            services.update_profile(request.user, profile_form)
            messages.success(request, "Saved.")
            return redirect("accounts:account")
    return render(request, "accounts/account.html", _account_context(request, profile_form=profile_form))


@login_required
@require_POST
def password_view(request):
    form = PasswordChangeForm(request.user, request.POST)
    if not form.is_valid():
        return render(request, "accounts/account.html", _account_context(request, password_form=form), status=400)
    old_key = request.session.session_key
    ended = services.change_password(request.user, form, keep_key=old_key)
    update_session_auth_hash(request, form.user)  # rotates this browser's session key
    UserSession.objects.filter(session_key=old_key).update(session_key=request.session.session_key)
    note = f" and signed out {ended} other session{'s' if ended != 1 else ''}" if ended else ""
    messages.success(request, f"Password changed{note}.")
    return redirect("accounts:account")


@login_required
@require_POST
def end_session_view(request, pk):
    row = get_object_or_404(UserSession, pk=pk, user=request.user)
    try:
        services.end_one_session(request.user, row, current_key=request.session.session_key)
    except services.AccountError as err:
        return HttpResponseBadRequest(err.message)
    messages.success(request, f"Signed out {row.device}.")
    return redirect("accounts:account")


@login_required
@require_POST
def end_other_sessions_view(request):
    ended = services.end_other_sessions(request.user, keep_key=request.session.session_key)
    messages.success(request, f"Signed out {ended} other session{'s' if ended != 1 else ''}.")
    return redirect("accounts:account")


@login_required
@require_POST
def tokens_view(request):
    """Make or revoke a personal API token (the same model the API uses)."""
    op = request.POST.get("op")
    if op == "create":
        token, raw = ApiToken.issue(request.user, request.POST.get("name", ""))
        log_activity(None, request.user, "account.token", f"made an API token “{token.name}” ({token.prefix}…)")
        request.session["new_api_token"] = raw
        messages.success(request, f"Token “{token.name}” made.")
    elif op == "revoke":
        token = get_object_or_404(ApiToken, pk=request.POST.get("id"), user=request.user, revoked_at__isnull=True)
        token.revoked_at = timezone.now()
        token.save(update_fields=["revoked_at"])
        log_activity(None, request.user, "account.token", f"revoked the API token “{token.name}” ({token.prefix}…)")
        messages.success(request, f"Revoked “{token.name}”.")
    else:
        return HttpResponseBadRequest("Unknown operation.")
    return redirect(f"{reverse('accounts:account')}#tokens")


# ---------------------------------------------------------------- admin ----

@admin_required
def admin_home(request):
    q = request.GET.get("q", "").strip()
    role = request.GET.get("role", "")
    users = User.objects.all()
    if q:
        users = users.filter(Q(email__icontains=q) | Q(name__icontains=q))
    if role in User.Role.values:
        users = users.filter(platform_role=role)
    elif role == "inactive":
        users = users.filter(is_active=False)
    page = Paginator(users.order_by("name", "email"), 40).get_page(request.GET.get("page"))

    events = Event.objects.annotate(
        team_count=Count("teams", distinct=True),
        submitted_count=Count(
            "projects",
            filter=Q(projects__status=Project.Status.SUBMITTED, projects__duplicate_of__isnull=True),
            distinct=True,
        ),
    ).order_by("-submissions_close_at")

    counts = {
        "users": User.objects.count(),
        "admins": User.objects.filter(platform_role=User.Role.ADMIN, is_active=True).count(),
        "organizers": User.objects.filter(platform_role=User.Role.ORGANIZER, is_active=True).count(),
        "inactive": User.objects.filter(is_active=False).count(),
    }
    return render(request, "accounts/admin.html", {
        "platform_log": Activity.objects.filter(event__isnull=True).select_related("actor")[:8],
        "page": page,
        "q": q,
        "role": role,
        "roles": User.Role.choices,
        "events": events,
        "counts": counts,
    })


@admin_required
@require_POST
def admin_user_update(request, pk):
    target = get_object_or_404(User, pk=pk)
    role = request.POST.get("platform_role", "")
    active = request.POST.get("is_active") == "on"
    back = safe_next(request, fallback="accounts:admin")
    if role not in User.Role.values:
        return HttpResponseBadRequest("Unknown role.")
    try:
        target, deactivated = services.admin_update(request.user, target, role, active)
    except services.AccountError as err:
        messages.error(request, err.message)
        return redirect(back)
    if deactivated:
        messages.success(request, f"{target.display_name} is deactivated and signed out everywhere.")
    else:
        messages.success(request, f"Updated {target.display_name}: {target.get_platform_role_display()}.")
    return redirect(back)


@admin_required
def admin_audit(request):
    """The platform audit log: entries that belong to no single event, such as
    sign-in lockouts, sign-up limits and changes to accounts."""
    entries, only, q = audit.filtered(Activity.objects.filter(event__isnull=True).select_related("actor"),
                                      request.GET.get("only", ""), request.GET.get("q", ""))
    page = Paginator(entries, 50).get_page(request.GET.get("page"))
    return render(request, "accounts/admin_audit.html", {
        "page": page, "only": only, "q": q,
        "categories": audit.category_links(["", "refused", "accounts"]),
    })

