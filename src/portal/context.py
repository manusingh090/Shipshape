from django.conf import settings

ORGANIZER_PAGES = {"organize", "create"}


def _section(request):
    match = getattr(request, "resolver_match", None)
    if match is None:
        return ""
    name, ns = match.url_name or "", match.namespace or ""
    if name == "home":
        return "events"
    if ns == "events":
        if name in ORGANIZER_PAGES or name.startswith("manage") or name == "promote_duplicate":
            return "organize"
        return "events"
    if ns == "projects" and name != "edit":
        return "gallery"
    if ns == "accounts" and name.startswith("admin"):
        return "admin"
    if ns == "integrity":
        return "organize"
    if ns == "transfer":
        return "organize"
    if ns == "embeds":
        return "organize" if name == "manage" else "gallery"
    if ns == "records":
        return "organize" if name == "manage" else "events"
    if ns == "webhooks":
        return "admin" if name == "admin" else "organize"
    if ns == "voting":
        return "organize" if name == "manage" else "events"
    if ns == "judging":
        if name in {"home", "queue", "score", "invite"}:
            return "judge"
        return "organize"
    return ""


def site(request):
    user = request.user
    signed_in = user.is_authenticated
    return {
        "SITE_NAME": settings.SITE_NAME,
        "DEMO_MODE": settings.DEMO_MODE,
        "DEMO_PASSWORD": settings.DEMO_PASSWORD if settings.DEMO_MODE else "",
        "nav": {
            "section": _section(request),
            "is_admin": signed_in and user.is_admin,
            "can_organize": signed_in and (
                user.can_create_events or user.event_roles.filter(role="organizer").exists()
            ),
            "is_judge": signed_in and user.event_roles.filter(role="judge").exists(),
        },
    }
