"""The embeddable gallery: the widget page, its loader script, a JSON feed,
and the organizer's Embed tab."""

from pathlib import Path

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.models import AnonymousUser
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.clickjacking import xframe_options_exempt

from accounts.decorators import login_required
from events.access import get_event_for, require_organizer
from events.models import Event
from portal.middleware import CONTENT_SECURITY_POLICY
from projects.api import serialize

from . import services

LOADER = Path(settings.BASE_DIR) / "static" / "js" / "embed.js"


def _embeddable(slug):
    event = get_object_or_404(Event, slug=slug)
    if not services.can_embed(event):
        raise Http404()
    return event


@xframe_options_exempt
def widget(request, slug):
    """The page that goes in the iframe. Rendered as a signed-out visitor
    would see it, whoever is signed in here, and framed only where the
    organizer allows."""
    event = _embeddable(slug)
    request.user = AnonymousUser()
    opts = services.options(request.GET)
    chosen, total = services.projects(event, opts)
    gallery = request.build_absolute_uri(reverse("projects:event_gallery", args=[event.slug]))
    response = render(request, "embeds/widget.html", {
        "event": event, "projects": chosen, "total": total, "opts": opts,
        "gallery_url": gallery + ("?" + services.query(opts, keep=("track", "tag", "q")) if services.query(opts, keep=("track", "tag", "q")) else ""),
        "event_url": request.build_absolute_uri(event.get_absolute_url()), "base": request.build_absolute_uri("/").rstrip("/"),
    })
    response["Content-Security-Policy"] = CONTENT_SECURITY_POLICY.replace(
        "frame-ancestors 'none'", f"frame-ancestors {services.frame_ancestors(event)}")
    response["Cache-Control"] = "no-store"
    return response


def loader(request):
    """The one-line embed: finds <div data-shipshape-gallery> on the host page
    and puts a self-sizing iframe in it."""
    response = HttpResponse(LOADER.read_text(encoding="utf-8"), content_type="text/javascript; charset=utf-8")
    response["Cache-Control"] = "public, max-age=3600"
    response["Access-Control-Allow-Origin"] = "*"
    return response


def feed(request, slug):
    """The same projects as JSON, readable from any site's JavaScript, for
    people who'd rather draw their own widget. Public data only: it sends
    no credentials and ignores who's signed in."""
    event = _embeddable(slug)
    request.user = AnonymousUser()
    opts = services.options(request.GET)
    chosen, total = services.projects(event, opts)
    response = JsonResponse({
        "event": {"slug": event.slug, "name": event.name, "url": request.build_absolute_uri(event.get_absolute_url())},
        "count": total,
        "projects": [{**serialize(p, request, full=False), "won": p.won} for p in chosen],
    })
    response["Access-Control-Allow-Origin"] = "*"
    response["Cache-Control"] = "no-store"
    return response


@login_required
def manage(request, slug):
    event, viewer = get_event_for(request, slug)
    require_organizer(viewer)
    config = services.settings_for(event, create=True)
    form = services.EmbedSettingsForm(instance=config)
    if request.method == "POST":
        form = services.EmbedSettingsForm(request.POST, instance=config)
        if form.is_valid():
            services.save(request.user, event, form)
            messages.success(request, "Embedding settings saved.")
            return redirect(f"{reverse('embeds:manage', args=[slug])}?{request.GET.urlencode()}")
    opts = services.options(request.GET)
    return render(request, "embeds/manage.html", {
        "event": event, "viewer": viewer, "section": "embed", "form": form, "config": config, "opts": opts,
        "snippets": services.snippets(request, event, opts), "tracks": event.tracks.all(),
        "sorts": services.SORTS, "themes": services.THEMES,
        "preview_url": reverse("embeds:widget", args=[slug]) + ("?" + services.query(opts) if services.query(opts) else ""),
    })
