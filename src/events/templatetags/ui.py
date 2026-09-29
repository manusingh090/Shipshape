from urllib.parse import urlparse

from django import template
from django.utils import timezone
from django.utils.html import format_html
from django.utils.safestring import mark_safe

from events.markdown import render as render_markdown
from events.timeutil import UTC, format_date, format_dt, humanize_delta, iso_utc

register = template.Library()


@register.simple_tag
def when(dt, event=None, style="full"):
    """A <time> element in the event's zone. site.js adds the reader's own
    local time as a tooltip."""
    if not dt:
        return ""
    tz = event.tzinfo if event is not None else UTC
    if style == "date":
        text = format_date(dt, tz)
    elif style == "short":
        text = format_dt(dt, tz, year=False)
    else:
        text = format_dt(dt, tz)
    return format_html('<time datetime="{}" data-local>{}</time>', iso_utc(dt), text)


@register.filter
def iso(dt):
    return iso_utc(dt) if dt else ""


@register.filter
def markdown(text):
    return mark_safe(render_markdown(text or ""))


@register.filter
def track_class(track):
    return f"t{track.colour_index}" if track else "t-none"


@register.filter
def cover_variant(project):
    return f"v{(project.pk or 0) % 4}"


@register.filter
def hostname(url):
    try:
        host = urlparse(url).hostname or ""
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


@register.filter
def until(dt):
    if not dt:
        return ""
    return humanize_delta(dt - timezone.now())


@register.filter
def since(dt):
    if not dt:
        return ""
    return humanize_delta(timezone.now() - dt)


@register.filter
def day_number(dt, event=None):
    tz = event.tzinfo if event is not None else UTC
    return f"{dt.astimezone(tz).day:02d}"


@register.filter
def month_year(dt, event=None):
    tz = event.tzinfo if event is not None else UTC
    return dt.astimezone(tz).strftime("%b %Y")


@register.simple_tag(takes_context=True)
def query(context, **changes):
    """Current query string with some keys replaced. Empty values drop the key.
    Changing any filter resets the page number."""
    params = context["request"].GET.copy()
    if "page" not in changes:
        params.pop("page", None)
    for key, value in changes.items():
        if value in (None, ""):
            params.pop(key, None)
        else:
            params[key] = value
    encoded = params.urlencode()
    return f"?{encoded}" if encoded else "?"


@register.filter
def field_type(bound_field):
    return bound_field.field.widget.__class__.__name__.lower()
