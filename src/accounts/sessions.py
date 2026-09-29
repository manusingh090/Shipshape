from django.contrib.sessions.models import Session

from .models import UserSession


def end_sessions(user, keep_key=None):
    """Sign a user out of every browser except, optionally, the current one."""
    rows = UserSession.objects.filter(user=user)
    if keep_key:
        rows = rows.exclude(session_key=keep_key)
    keys = list(rows.values_list("session_key", flat=True))
    Session.objects.filter(session_key__in=keys).delete()
    rows.delete()
    return len(keys)


def end_session(user, session_key):
    row = UserSession.objects.filter(user=user, session_key=session_key).first()
    if row is None:
        return False
    Session.objects.filter(session_key=session_key).delete()
    row.delete()
    return True
