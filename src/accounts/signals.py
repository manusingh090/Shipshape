from django.contrib.auth.signals import user_logged_in, user_logged_out
from django.dispatch import receiver
from django.utils import timezone

from .models import UserSession
from .throttle import client_ip


@receiver(user_logged_in)
def remember_session(sender, request, user, **kwargs):
    if request is None or not hasattr(request, "session"):
        return
    if not request.session.session_key:
        request.session.save()
    now = timezone.now()
    UserSession.objects.update_or_create(
        session_key=request.session.session_key,
        defaults={
            "user": user,
            "user_agent": request.headers.get("User-Agent", "")[:300],
            "ip": client_ip(request),
            "created_at": now,
            "last_seen_at": now,
            "label": "",
        },
    )


@receiver(user_logged_out)
def forget_session(sender, request, user, **kwargs):
    if request is None or not hasattr(request, "session"):
        return
    key = request.session.session_key
    if key:
        UserSession.objects.filter(session_key=key).delete()
