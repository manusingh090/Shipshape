"""JSON shapes for the REST API. Plain functions, one per kind of thing.

What a caller sees still depends on who they are: the endpoints decide which
objects to hand these functions (a participant never gets another team's
invite code, a judge never gets another judge's score).
"""

from events.timeutil import iso_utc


def when(dt):
    return iso_utc(dt) if dt else None


def person(user, email=False):
    if user is None:
        return None
    data = {"id": user.pk, "name": user.display_name}
    if email:
        data["email"] = user.email
    return data


def event(e, viewer=None):
    data = {
        "slug": e.slug, "name": e.name, "tagline": e.tagline, "description": e.description,
        "location": e.location, "timezone": e.timezone, "phase": e.phase(),
        "starts_at": when(e.starts_at), "submissions_close_at": when(e.submissions_close_at),
        "judging_ends_at": when(e.judging_ends_at), "results_at": when(e.results_at),
        "max_team_size": e.max_team_size, "is_published": e.is_published,
        "comments_enabled": e.comments_enabled,
        "gallery_before_deadline": e.gallery_before_deadline,
        "tracks": [track(t) for t in e.tracks.all()],
        "prizes": [prize(p) for p in e.prizes.select_related("track")],
    }
    if viewer is not None:
        data["you"] = {"roles": [label.lower() for label in viewer.role_labels],
                       "team": {"id": viewer.team.pk, "name": viewer.team.name} if viewer.team else None}
    return data


def track(t):
    return {"id": t.pk, "name": t.name, "description": t.description, "position": t.position}


def prize(p):
    return {"id": p.pk, "name": p.name, "value": p.value, "description": p.description, "quantity": p.quantity,
            "position": p.position, "track": p.track_id}


def question(q):
    return {"id": q.pk, "prompt": q.prompt, "help_text": q.help_text, "kind": q.kind, "options": q.option_list,
            "required": q.required, "is_public": q.is_public, "position": q.position}


def team(t, request=None, with_invite=False):
    data = {
        "id": t.pk, "event": t.event.slug, "name": t.name, "created_at": when(t.created_at),
        "members": [{"id": m.user_id, "name": m.user.display_name, "role": m.role, "joined_at": when(m.joined_at)}
                    for m in t.memberships.select_related("user").order_by("joined_at")],
    }
    if with_invite:
        data["invite_code"] = t.invite_code
        if request is not None:
            data["invite_url"] = request.build_absolute_uri(t.get_invite_url())
    return data


def comment(row):
    c = row["comment"]
    if row["hidden"]:
        return {"id": c.pk, "removed": True, "note": "Removed by the organizers."}
    data = {"id": c.pk, "author": person(c.author), "badge": row["badge"] or None, "yours": row["mine"],
            "body": c.body, "created_at": when(c.created_at), "removed": c.removed_at is not None}
    if c.removed_at is not None:
        data.update({"removed_at": when(c.removed_at), "removed_by": person(c.removed_by),
                     "removal_reason": c.removal_reason})
    return data


def criterion(c):
    return {"id": c.pk, "key": c.key, "label": c.label, "description": c.description,
            "weight": float(c.weight), "position": c.position}


def judge_role(role, progress=None):
    data = {"id": role.pk, "user": person(role.user, email=True), "all_tracks": role.all_tracks,
            "tracks": [t.pk for t in role.tracks.all()]}
    if progress is not None:
        data.update({"assigned": progress.assigned, "submitted": progress.submitted, "drafts": progress.drafts,
                     "status": progress.status})
    return data


def invite(i, request=None):
    from django.urls import reverse

    data = {"id": i.pk, "email": i.email or None, "state": i.state, "all_tracks": i.all_tracks,
            "tracks": [t.pk for t in i.tracks.all()], "expires_at": when(i.expires_at),
            "accepted_by": person(i.accepted_by)}
    if request is not None and i.state == "open":
        data["url"] = request.build_absolute_uri(reverse("judging:invite", args=[i.token]))
        data["token"] = i.token
    return data


def assignment(a):
    score = getattr(a, "score", None)
    state = "submitted" if score and score.submitted_at else ("draft" if score else "pending")
    return {"id": a.pk, "judge": person(a.judge), "project": {"id": a.project_id, "title": a.project.title},
            "batch": a.batch_id, "status": state, "created_at": when(a.created_at)}


def batch(b):
    return {"id": b.pk, "label": b.label, "mode": b.mode, "scope": b.scope, "seed": b.seed,
            "target_reviews": b.target_reviews, "shortfall": b.shortfall_count, "created_at": when(b.created_at)}


def activity(a):
    return {"id": a.pk, "at": when(a.created_at), "type": a.verb, "actor": person(a.actor), "detail": a.detail,
            "event": a.event.slug if a.event_id else None, "team": a.team_id, "project": a.project_id,
            "refused": a.is_refusal}


def voting_config(c):
    return {"enabled": c.is_enabled, "access": c.access, "method": c.method, "credits": c.credits,
            "max_votes": c.max_votes, "order": c.order, "opens_at": when(c.opens_at), "closes_at": when(c.closes_at),
            "email_domains": c.domain_list, "results_published_at": when(c.results_published_at)}


def user_admin(u):
    return {"id": u.pk, "name": u.display_name, "email": u.email, "platform_role": u.platform_role,
            "is_active": u.is_active, "date_joined": when(u.date_joined)}


def token(t):
    return {"id": t.pk, "name": t.name, "prefix": t.prefix, "created_at": when(t.created_at),
            "last_used_at": when(t.last_used_at), "revoked_at": when(t.revoked_at)}
