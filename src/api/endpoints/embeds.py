"""The embeddable gallery's settings, and the snippets to paste."""

from embeds import services

from .. import schema as sc
from ..core import endpoint, json_body, valid
from .events import event_for


# What each endpoint takes and returns (api/schema.py), for the OpenAPI document.
SNIPPET_QUERY = {"track": sc.integer("Only this track."), "tag": sc.string(), "q": sc.string("Search."),
                 "sort": sc.enum("shuffle", "title", "newest"), "limit": sc.integer("1 to 48.", minimum=1, maximum=48),
                 "theme": sc.enum("auto", "light", "dark")}
EMBED = sc.obj(enabled=sc.boolean(), allowed_origins=sc.array(sc.string(), "Sites that may embed it; empty means any."),
               frame_ancestors=sc.string("The widget's content security policy says this."),
               script=sc.string("The two lines to paste."), iframe=sc.string("Or a plain iframe."),
               widget_url=sc.url(), feed_url=sc.url("The same projects as JSON, readable from any site."))
EMBED_BODY = sc.obj(enabled=sc.optional(sc.boolean()),
                    allowed_origins=sc.optional(sc.any_of(sc.array(sc.string()), sc.string(),
                                                          description="Scheme and host, e.g. https://hack.example.org.")))


def _json(request, event):
    config = services.settings_for(event)
    opts = services.options(request.GET)
    return {"enabled": config.enabled, "allowed_origins": config.origin_list,
            "frame_ancestors": services.frame_ancestors(event), **services.snippets(request, event, opts)}


@endpoint("GET", "events/<slug:slug>/embed", who="organizer",
          summary="Embedding settings and the snippets to paste. ?track=, ?tag=, ?sort=, ?limit=, ?theme= shape the snippet.",
          ui="Console, Embed",
          query=SNIPPET_QUERY, returns=EMBED)
def get_embed(request, slug):
    event, _ = event_for(request, slug, organizer=True)
    return _json(request, event)


@endpoint("PATCH", "events/<slug:slug>/embed", who="organizer",
          summary="Switch embedding on or off, or limit it to some sites.", ui="Console, Embed, Who may embed it",
          body='{"enabled": true, "allowed_origins": ["https://hackathon.example.org"]}',
          request=EMBED_BODY, returns=EMBED)
def update_embed(request, slug):
    event, _ = event_for(request, slug, organizer=True)
    config = services.settings_for(event, create=True)
    body = json_body(request)
    origins = body.get("allowed_origins", config.origin_list)
    data = {"enabled": body.get("enabled", config.enabled),
            "allowed_origins": "\n".join(origins) if isinstance(origins, list) else str(origins or "")}
    services.save(request.user, event, valid(services.EmbedSettingsForm(data, instance=config)))
    return _json(request, event)
