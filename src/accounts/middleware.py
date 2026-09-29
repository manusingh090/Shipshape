from datetime import timedelta

from django.utils import timezone

from .models import UserSession
from .throttle import client_ip

TOUCH_EVERY = timedelta(minutes=5)


class SessionActivityMiddleware:
    """Keeps UserSession.last_seen_at roughly current (at most one write per
    five minutes per browser) so the account page can show where you are
    signed in."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        user = getattr(request, "user", None)
        session = getattr(request, "session", None)
        if user is not None and user.is_authenticated and session is not None and session.session_key:
            self.touch(request, user, session.session_key)
        return response

    def touch(self, request, user, key):
        now = timezone.now()
        row = UserSession.objects.filter(session_key=key).only("id", "last_seen_at").first()
        if row is None:
            UserSession.objects.create(
                user=user,
                session_key=key,
                user_agent=request.headers.get("User-Agent", "")[:300],
                ip=client_ip(request),
            )
        elif row.last_seen_at < now - TOUCH_EVERY:
            UserSession.objects.filter(pk=row.pk).update(last_seen_at=now, ip=client_ip(request))
