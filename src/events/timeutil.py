from datetime import timezone as dt_timezone

UTC = dt_timezone.utc


def localize(dt, tz):
    return dt.astimezone(tz or UTC)


def format_dt(dt, tz=None, year=True):
    """1 Mar 2026, 18:00 UTC. Built by hand because %-d is not portable."""
    if dt is None:
        return ""
    local = localize(dt, tz)
    date = f"{local.day} {local:%b}" + (f" {local:%Y}" if year else "")
    return f"{date}, {local:%H:%M} {local.tzname() or ''}".strip()


def format_date(dt, tz=None):
    if dt is None:
        return ""
    local = localize(dt, tz)
    return f"{local.day} {local:%b %Y}"


def iso_utc(dt):
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def humanize_delta(delta):
    seconds = int(abs(delta.total_seconds()))
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days >= 60:
        months = days // 30
        return f"{months} months"
    if days >= 2:
        return f"{days} days"
    if days == 1:
        return f"1 day, {hours} hour{'s' if hours != 1 else ''}" if hours else "1 day"
    if hours:
        return f"{hours} h {minutes} min" if minutes else f"{hours} hour{'s' if hours != 1 else ''}"
    if minutes:
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    return "less than a minute"
