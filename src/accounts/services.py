"""Account changes, shared by the pages and the REST API.

Security-relevant ones (a new account, a password change, being signed out
of other browsers, an admin changing someone's role) go into the platform
audit log, which admins read at /admin/audit/.
"""

from events.models import EventRole, log_activity
from integrity import limits

from .models import User
from .sessions import end_session, end_sessions


class AccountError(Exception):
    status = 400
    code = "account_error"

    def __init__(self, message):
        super().__init__(message)
        self.message = message


def sign_up(form, network):
    """form: a valid SignupForm. Raises limits.Limited when one network has
    made too many accounts this hour."""
    limits.check("signup.network", network,
                 "A lot of accounts have been made from this network in the last hour. Try again later.",
                 detail=f"network {network[:10]}")
    user = form.save()
    limits.record("signup.network", network)
    log_activity(None, user, "account.created", f"made an account ({user.email})")
    return user


def update_profile(user, form):
    old = type(user).objects.get(pk=user.pk).name  # the form already set the new one on user
    user = form.save()
    if old != user.name:
        log_activity(None, user, "account.updated", f"changed their name from “{old}” to “{user.name}”")
    return user


def change_password(user, form, keep_key=None):
    """form: a valid PasswordChangeForm. Ends every other session, keeping
    keep_key (the browser that asked). Returns how many were ended."""
    from api.models import ApiToken
    from django.utils import timezone

    user = form.save()
    ended = end_sessions(user, keep_key=keep_key)
    revoked = ApiToken.objects.filter(user=user, revoked_at__isnull=True).update(revoked_at=timezone.now())
    tokens = f" and revoked {revoked} API token{'s' if revoked != 1 else ''}" if revoked else ""
    log_activity(None, user, "account.password",
                 f"changed their password, signed out {ended} other session{'s' if ended != 1 else ''}{tokens}")
    return ended


def end_one_session(user, row, current_key=None):
    if row.session_key == current_key:
        raise AccountError("Use Sign out to end the session you are using.")
    end_session(user, row.session_key)
    log_activity(None, user, "account.sessions", f"signed out {row.device or 'a browser'}")


def end_other_sessions(user, keep_key=None):
    ended = end_sessions(user, keep_key=keep_key)
    if ended:
        log_activity(None, user, "account.sessions",
                     f"signed out {ended} other session{'s' if ended != 1 else ''}")
    return ended


def admin_update(actor, target, role, active):
    """An admin changes someone's platform role or deactivates them."""
    if role not in User.Role.values:
        raise AccountError("Unknown role.")
    judging = target.event_roles.filter(role=EventRole.Role.JUDGE).count()
    if role == User.Role.ADMIN and not target.is_admin and judging:
        raise AccountError(
            f"{target.display_name} is judging {judging} event{'s' if judging != 1 else ''}. Admins can see every "
            "score, so remove them as a judge first.")
    losing_admin = target.is_admin and (role != User.Role.ADMIN or not active)
    if losing_admin and target == actor:
        raise AccountError("You can't remove your own admin access. Ask another admin to do it.")
    if losing_admin and User.objects.filter(platform_role=User.Role.ADMIN, is_active=True).count() <= 1:
        raise AccountError("That's the last admin. Make someone else an admin first.")

    was_active, was_role = target.is_active, target.get_platform_role_display()
    target.platform_role = role
    target.is_active = active
    target.save(update_fields=["platform_role", "is_active"])
    if was_role != target.get_platform_role_display():
        log_activity(None, actor, "admin.account",
                     f"made {target.display_name} ({target.email}) {target.get_platform_role_display().lower()}, "
                     f"was {was_role.lower()}")
    if was_active != active:
        log_activity(None, actor, "admin.account",
                     f"{'deactivated' if not active else 'reactivated'} {target.display_name} ({target.email})")
    if was_active and not active:
        end_sessions(target)
        # Their API tokens stop working too: deactivated accounts can't sign in by any door.
    return target, was_active and not active
