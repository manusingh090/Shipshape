from functools import wraps

from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied


def login_required(view):
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path())
        return view(request, *args, **kwargs)

    return wrapper


def admin_required(view):
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path())
        if not request.user.is_admin:
            raise PermissionDenied("Only platform admins can open this page.")
        return view(request, *args, **kwargs)

    return wrapper


def event_creator_required(view):
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path())
        if not request.user.can_create_events:
            raise PermissionDenied(
                "Creating a hackathon needs the organizer role. An admin can give it to your account."
            )
        return view(request, *args, **kwargs)

    return wrapper
