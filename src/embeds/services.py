"""The embeddable gallery's rules: what it shows, where it may be framed.

What it shows is what a signed-out visitor sees in the gallery, always,
whoever is signed in to the portal in that browser: a widget on someone
else's site must never show an organizer their drafts, or anyone a count
that's still hidden. No votes, no scores. Winners show once awards are public.
"""

import random
from urllib.parse import urlencode, urlsplit

from django import forms
from django.db.models.functions import Lower
from django.urls import reverse

from events.models import log_activity
from projects.views import listed_projects, search_projects
from records.services import public_awards

from .models import EmbedSettings

SORTS = {"shuffle": "Shuffled each time it loads", "title": "A to Z", "newest": "Newest first"}
THEMES = {"auto": "Match the visitor's light or dark setting", "light": "Light", "dark": "Dark"}
MAX_LIMIT = 48


def settings_for(event, create=False):
    if create:
        return EmbedSettings.objects.get_or_create(event=event)[0]
    return EmbedSettings.objects.filter(event=event).first() or EmbedSettings(event=event)


def can_embed(event):
    return event.is_published and settings_for(event).enabled


def frame_ancestors(event):
    """The CSP frame-ancestors value for this event's widget. The portal can
    always frame it (for the organizer's preview)."""
    origins = settings_for(event).origin_list
    return "'self' " + (" ".join(origins) if origins else "*")


def options(params):
    """Clean the widget's options from a query string."""
    sort = params.get("sort", "shuffle")
    theme = params.get("theme", "auto")
    try:
        limit = max(1, min(MAX_LIMIT, int(params.get("limit", 12))))
    except (TypeError, ValueError):
        limit = 12
    track = params.get("track", "")
    return {"sort": sort if sort in SORTS else "shuffle", "theme": theme if theme in THEMES else "auto",
            "limit": limit, "track": track if str(track).isdigit() else "",
            "tag": (params.get("tag", "") or "").strip().lower()[:40],
            "q": (params.get("q", "") or "").strip()[:100], "id": (params.get("id", "") or "")[:40]}


def projects(event, opts):
    """(the projects to show, how many match in all)."""
    base = listed_projects().filter(event=event)
    matching = search_projects(base, opts["q"], opts["track"], opts["tag"])
    total = matching.count()
    if opts["sort"] == "shuffle":
        chosen = list(matching)
        random.shuffle(chosen)  # fresh each load: nobody is always first on the host page
        chosen = chosen[:opts["limit"]]
    else:
        order = [Lower("title"), "id"] if opts["sort"] == "title" else ["-submitted_at", "id"]
        chosen = list(matching.order_by(*order)[:opts["limit"]])
    winners = {}
    for award in public_awards(event):
        winners.setdefault(award.project_id, []).append(award.prize.name)
    for p in chosen:
        p.won = winners.get(p.pk, [])
    return chosen, total


def query(opts, keep=("track", "tag", "q", "sort", "limit", "theme")):
    return urlencode({k: opts[k] for k in keep if opts.get(k) and not (k == "sort" and opts[k] == "shuffle")
                      and not (k == "theme" and opts[k] == "auto") and not (k == "limit" and opts[k] == 12)})


def snippets(request, event, opts):
    """The two ways to put it on a page: a script that sizes itself, or a plain iframe."""
    base = request.build_absolute_uri("/").rstrip("/")
    widget = base + reverse("embeds:widget", args=[event.slug])
    qs = query(opts)
    attrs = "".join(f' data-{k}="{opts[k]}"' for k in ("track", "tag", "q", "sort", "limit", "theme")
                    if opts.get(k) and not (k == "sort" and opts[k] == "shuffle")
                    and not (k == "theme" and opts[k] == "auto") and not (k == "limit" and opts[k] == 12))
    script = (f'<div data-shipshape-gallery="{event.slug}"{attrs}></div>\n'
              f'<script src="{base}{reverse("embeds:loader")}" async></script>')
    iframe = (f'<iframe src="{widget}{"?" + qs if qs else ""}" title="Projects from {event.name}" '
              f'width="100%" height="640" loading="lazy" style="border:0"></iframe>')
    return {"script": script, "iframe": iframe, "widget_url": widget + ("?" + qs if qs else ""),
            "feed_url": base + reverse("embeds:feed", args=[event.slug])}


class EmbedSettingsForm(forms.ModelForm):
    class Meta:
        model = EmbedSettings
        fields = ["enabled", "allowed_origins"]
        widgets = {"allowed_origins": forms.Textarea(attrs={"rows": 3,
                                                            "placeholder": "https://hackathon.example.org"})}

    def clean_allowed_origins(self):
        cleaned = []
        for line in self.cleaned_data["allowed_origins"].splitlines():
            line = line.strip().rstrip("/")
            if not line:
                continue
            parts = urlsplit(line)
            if parts.scheme not in ("http", "https") or not parts.netloc or parts.path or parts.query:
                raise forms.ValidationError(f"“{line}” isn't a site address. Write it like https://example.org, "
                                            "with no path.")
            cleaned.append(f"{parts.scheme}://{parts.netloc}")
        return "\n".join(cleaned)


def save(actor, event, form):
    config = form.save(commit=False)
    config.event = event
    config.save()
    where = ", ".join(config.origin_list) or "any site"
    log_activity(event, actor, "event.embed",
                 f"{'allowed' if config.enabled else 'stopped'} embedding the gallery{f' on {where}' if config.enabled else ''}")
    return config
