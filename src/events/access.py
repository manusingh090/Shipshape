"""Who may do what. Every view asks these helpers; templates only read them.

Hiding a button is not a permission check. Views call require_* (or raise
Http404 via get_event_for) before touching data, so a hand-typed URL or a
curl request meets the same refusal as the UI.
"""

from django.core.exceptions import PermissionDenied
from django.db.models import Q
from django.http import Http404
from django.shortcuts import get_object_or_404

from .models import Event, EventRole


class Viewer:
    """The current user's standing in one event, worked out once per request."""

    def __init__(self, user, event):
        from teams.models import Membership

        self.user = user
        self.event = event
        self.signed_in = user.is_authenticated
        self.is_admin = self.signed_in and user.is_admin
        roles = set()
        if self.signed_in:
            roles = set(EventRole.objects.filter(event=event, user=user).values_list("role", flat=True))
        self.is_event_organizer = EventRole.Role.ORGANIZER in roles
        self.is_judge = EventRole.Role.JUDGE in roles
        self.is_organizer = self.is_admin or self.is_event_organizer
        self.membership = (
            Membership.objects.select_related("team").filter(event=event, user=user).first()
            if self.signed_in
            else None
        )

    @property
    def team(self):
        return self.membership.team if self.membership else None

    @property
    def is_participant(self):
        return self.membership is not None

    @property
    def is_staff(self):
        """Organizers and judges of this event, and platform admins (who can
        manage every event), may not also compete in it."""
        return self.is_event_organizer or self.is_judge or self.is_admin

    @property
    def can_view_event(self):
        return self.event.is_published or self.is_organizer

    @property
    def role_labels(self):
        labels = []
        if self.is_admin:
            labels.append("Admin")
        if self.is_event_organizer:
            labels.append("Organizer")
        if self.is_judge:
            labels.append("Judge")
        if self.is_participant:
            labels.append("Participant")
        return labels


def get_event_for(request, slug):
    """Load an event the current user is allowed to see, or 404.

    Unpublished events 404 rather than 403 for outsiders, so their existence
    does not leak.
    """
    event = get_object_or_404(Event, slug=slug)
    viewer = Viewer(request.user, event)
    if not viewer.can_view_event:
        raise Http404()
    return event, viewer


def require_organizer(viewer):
    if not viewer.is_organizer:
        raise PermissionDenied("Only this event's organizers can do that.")


def visible_events(user):
    qs = Event.objects.all()
    if user.is_authenticated and user.is_admin:
        return qs
    if user.is_authenticated:
        return qs.filter(
            Q(is_published=True) | Q(roles__user=user, roles__role=EventRole.Role.ORGANIZER)
        ).distinct()
    return qs.filter(is_published=True)


def organizer_events(user):
    if user.is_admin:
        return Event.objects.all()
    return Event.objects.filter(roles__user=user, roles__role=EventRole.Role.ORGANIZER).distinct()


def can_view_project(user, project, viewer=None):
    """Public once submitted in a published event whose gallery is open.
    Drafts, flagged duplicates and projects waiting for the deadline are
    visible only to the team and the event's organizers."""
    event = project.event
    publicly_listed = (
        project.status == project.Status.SUBMITTED
        and project.duplicate_of_id is None
        and event.is_published
        and event.gallery_open()
    )
    if publicly_listed:
        return True
    if not user.is_authenticated:
        return False
    viewer = viewer or Viewer(user, event)
    if viewer.is_organizer:
        return True
    return project.team.memberships.filter(user=user).exists()
