"""JSON Schema for the REST API: a few helpers to write schemas briefly,
the shared shapes (one per serializer), and a small validator.

The schemas here go into the OpenAPI document (api/openapi.py) and, during
the test run, every /api/v1/ response is checked against the schema its
endpoint declares (api/core._check_contract). Objects don't allow keys
they don't list, so a field added to a serializer without being documented
fails the tests that return it.

The validator covers the part of JSON Schema these schemas use: type (one
or a list), properties, required, additionalProperties, items, enum, anyOf,
$ref, minimum and maximum, maxLength, and the date-time and uri formats.
"""

from datetime import datetime

REF = "#/components/schemas/"
COMPONENTS = {}


# ----------------------------------------------------------------- helpers

def string(description="", **kw):
    return _describe({"type": "string", **kw}, description)


def integer(description="", **kw):
    return _describe({"type": "integer", **kw}, description)


def number(description="", **kw):
    return _describe({"type": "number", **kw}, description)


def boolean(description=""):
    return _describe({"type": "boolean"}, description)


def when(description=""):
    """A moment, as ISO 8601 in UTC ("2026-03-01T18:00:00Z")."""
    return _describe({"type": "string", "format": "date-time"}, description)


def url(description=""):
    return _describe({"type": "string", "format": "uri"}, description)


def enum(*values, description=""):
    kind = "integer" if all(isinstance(v, int) and not isinstance(v, bool) for v in values) else "string"
    return _describe({"type": kind, "enum": list(values)}, description)


def nullable(schema):
    if "$ref" in schema or "anyOf" in schema:
        return {"anyOf": [schema, {"type": "null"}]}
    kinds = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
    out = {**schema, "type": kinds + ["null"]}
    if "enum" in out:
        out["enum"] = out["enum"] + [None]
    return out


def array(items, description=""):
    return _describe({"type": "array", "items": items}, description)


def mapping(values, description=""):
    """An object used as a dictionary: any keys, values of one shape."""
    return _describe({"type": "object", "additionalProperties": values}, description)


class optional:
    """Marks a property that isn't always present."""

    def __init__(self, schema):
        self.schema = schema


def obj(_description="", _open=False, **properties):
    """An object. Every property is required unless wrapped in optional();
    keys that aren't listed are refused unless _open."""
    props, required = {}, []
    for name, schema in properties.items():
        if isinstance(schema, optional):
            props[name] = schema.schema
        else:
            props[name] = schema
            required.append(name)
    out = {"type": "object", "properties": props, "additionalProperties": bool(_open)}
    if required:
        out["required"] = required
    return _describe(out, _description)


def any_of(*schemas, description=""):
    return _describe({"anyOf": list(schemas)}, description)


def ref(name):
    """A reference to a shared shape. Endpoint modules may register shapes
    after this is called; tests/test_openapi checks that every one resolves."""
    return {"$ref": REF + name}


def component(name, schema):
    COMPONENTS[name] = schema
    return ref(name)


def page(item):
    """What paginate() returns: one page of a list (?page=, ?per_page=)."""
    return {**obj("One page of results. Ask for more with ?page= and ?per_page= (at most 200).",
                  count=integer("How many there are in all."), page=integer("This page's number, from 1."),
                  pages=integer("How many pages there are."), results=array(item)),
            "x-shipshape-paginated": True}


def results(item, description=""):
    return obj(description, results=array(item))


def _describe(schema, description):
    if description:
        schema = {**schema, "description": description}
    return schema


# ---------------------------------------------------------- shared shapes

ID = integer("The object's id.")
component("Person", obj("Someone, as others may see them.", id=integer(), name=string()))
component("PersonWithEmail", obj("Someone, with their address: only shown to organizers and to themselves.",
                                  id=integer(), name=string(), email=string(format="email")))
component("Track", obj(id=ID, name=string(), description=string(), position=integer()))
component("Prize", obj(id=ID, name=string(), value=string(), description=string(), quantity=integer(),
                       position=integer(), track=nullable(integer("The track this prize belongs to, if any."))))
component("Question", obj(
    "A question the organizers added to the submission form.",
    id=ID, prompt=string(), help_text=string(), kind=enum("short", "long", "choice", "yesno", "url"),
    options=array(string(), "The choices, for kind=choice."), required=boolean(),
    is_public=boolean("Whether answers show on the public project page."), position=integer()))
component("Event", obj(
    slug=string("The event's address, e.g. sample-hack-2026."), name=string(), tagline=string(),
    description=string("Markdown."), location=string(), timezone=string("An IANA zone, e.g. Asia/Kolkata."),
    phase=enum("upcoming", "open", "closed", description="Before kickoff, while submissions are open, after the deadline."),
    starts_at=when("Kickoff."), submissions_close_at=when("The deadline. The instant itself counts as closed."),
    judging_ends_at=nullable(when()), results_at=nullable(when("When winners are announced.")),
    max_team_size=integer(), is_published=boolean(), comments_enabled=boolean(),
    gallery_before_deadline=boolean("Whether submitted projects are public before the deadline."),
    tracks=array(ref("Track")), prizes=array(ref("Prize")),
    you=optional(obj("Your standing in this event, when you're signed in.",
                     roles=array(string()), team=nullable(obj(id=integer(), name=string()))))))
component("Member", obj(id=integer("The member's user id."), name=string(), role=enum("captain", "member"),
                        joined_at=when()))
component("Team", obj(
    id=ID, event=string("The event's slug."), name=string(), created_at=when(), members=array(ref("Member")),
    invite_code=optional(string("Only for the team's members and the event's organizers.")),
    invite_url=optional(url("The join link to send teammates."))))
component("ProjectSummary", obj(
    id=ID, url=url("The project's page."), event=string("The event's slug."), title=string(), tagline=string(),
    team=obj(id=integer(), name=string()), track=nullable(obj(id=integer(), name=string())),
    status=enum("draft", "submitted"), submitted_at=nullable(when()), updated_at=when()))
component("Project", obj(
    id=ID, url=url("The project's page."), event=string("The event's slug."), title=string(), tagline=string(),
    team=obj(id=integer(), name=string()), track=nullable(obj(id=integer(), name=string())),
    status=enum("draft", "submitted"), submitted_at=nullable(when()), updated_at=when(),
    description=string("Markdown."), repo_url=string(), live_url=string(), demo_video_url=string(),
    tags=array(string()),
    answers=array(obj(question_id=integer(), prompt=string(), value=string()),
                  "Answers to the form's questions. Private ones only for the team and organizers."),
    images=array(url()), thumbnail=nullable(url())))
component("Comment", any_of(
    obj("A comment the organizers removed, as the public sees it.",
        id=ID, removed={"type": "boolean", "enum": [True]}, note=string()),
    obj(id=ID, author=ref("Person"), badge=nullable(string("\"Team\" or \"Organizer\", when that applies.")),
        yours=boolean(), body=string("Markdown."), created_at=when(), removed=boolean(),
        removed_at=optional(nullable(when())), removed_by=optional(nullable(ref("Person"))),
        removal_reason=optional(string()))))
component("Criterion", obj(
    "One line of the rubric.", id=ID, key=string(), label=string(), description=string(),
    weight=number("Relative weight; the rubric's weights needn't add up to anything."), position=integer()))
component("JudgeRole", obj(
    id=ID, user=ref("PersonWithEmail"), all_tracks=boolean(), tracks=array(integer(), "Track ids."),
    assigned=optional(integer()), submitted=optional(integer()), drafts=optional(integer()),
    status=optional(enum("not_started", "in_progress", "unassigned", "done"))))
component("JudgeInvite", obj(
    id=ID, email=nullable(string()), state=enum("open", "accepted", "expired", "revoked"), all_tracks=boolean(),
    tracks=array(integer()), expires_at=nullable(when()), accepted_by=nullable(ref("Person")),
    url=optional(url("The invite link, while it's open.")), token=optional(string())))
component("Assignment", obj(
    id=ID, judge=ref("Person"), project=obj(id=integer(), title=string()), batch=nullable(integer()),
    status=enum("pending", "draft", "submitted"), created_at=when()))
component("Batch", obj(
    "One round of assignments.", id=ID, label=string(), mode=enum("algorithmic", "import"), scope=string(),
    seed=nullable(integer("The random seed, so the plan can be reproduced.")), target_reviews=integer(),
    shortfall=integer("Projects that couldn't get enough judges."), created_at=when()))
component("Activity", obj(
    "One line of the audit log.", id=ID, at=when(), type=string("What happened, e.g. project.submitted."),
    actor=nullable(ref("Person")), detail=string(), event=nullable(string()), team=nullable(integer()),
    project=nullable(integer()), refused=boolean("A refused attempt: a late edit, a rate limit.")))
component("VotingConfig", obj(
    enabled=boolean(), access=enum("link", "email", "account"), method=enum("quadratic", "single"),
    credits=integer(), max_votes=integer("The most votes one ballot may give one project."),
    order=enum("shuffled", "alphabetical"), opens_at=nullable(when()), closes_at=nullable(when()),
    email_domains=array(string()), results_published_at=nullable(when())))
component("UserAdmin", obj(id=ID, name=string(), email=string(), platform_role=enum("member", "organizer", "admin"),
                           is_active=boolean(), date_joined=when()))
component("Token", obj(id=ID, name=string(), prefix=string("The token's first characters, to recognise it."),
                       created_at=when(), last_used_at=nullable(when()), revoked_at=nullable(when())))
component("Error", obj(
    "What every refusal looks like. `error` is stable, for programs; `detail` is written for people.",
    _open=True, error=string("A short code, e.g. submissions_closed, not_found, slow_down."),
    detail=optional(string()),
    fields=optional(mapping(array(string()), "Per field, what's wrong with it.")),
    missing=optional(array(string(), "What's still needed before submitting."))))
DELETED = obj(deleted=string("The name of what was deleted."))


# --------------------------------------------------------------- forms

def form_schema(form_class, partial=False, extra=None, drop=(), required=None, optional_fields=(), **init):
    """The JSON body an endpoint accepts, from the Django form it validates
    with, so the documentation can't drift from what the form enforces.

    Returns a function: the form is built when the OpenAPI document is, not
    at import time, because many forms add fields in __init__ from the event
    (its tracks, its judges). event=True builds it for a placeholder event.
    partial: a PATCH, where every field is optional. extra: {name: (schema,
    required)} for what the endpoint takes beside the form's fields.
    required / optional_fields: where the endpoint fills in a default."""
    def build():
        from django import forms

        kwargs = dict(init)
        if kwargs.get("event") is True:
            kwargs["event"] = placeholder_event()
        form = form_class(**kwargs)
        props = {}
        for name, field in form.fields.items():
            if name in drop or isinstance(field, forms.FileField):
                continue
            must = field.required and not field.disabled and name not in optional_fields
            if required is not None:
                must = name in required
            props[name] = (_field(name, field), must and not partial)
        for name, (schema, needed) in (extra or {}).items():
            props[name] = (schema, needed and not partial)
        out = {"type": "object", "properties": {k: v for k, (v, _) in props.items()}, "additionalProperties": False}
        names = [k for k, (_, r) in props.items() if r]
        if names:
            out["required"] = names
        return out
    return build


def placeholder_event():
    """An event that looks saved, for building forms that need one. Their
    querysets are lazy and never run while the schema is made."""
    from events.models import Event

    event = Event(pk=0, slug="example", name="Example", timezone="UTC")
    event._state.adding = False
    return event


def resolve(schema):
    return schema() if callable(schema) else schema


def _field(name, field):
    from django import forms

    description = str(field.help_text or field.label or "").strip()
    if description.rstrip(".").lower() == name.replace("_", " ").lower():
        description = ""  # a label that only repeats the name says nothing
    if field.disabled:
        description = (description + " " if description else "") + "(Read only here.)"
    if isinstance(field, forms.ModelMultipleChoiceField):
        schema = array(integer(), description or f"The {name.replace('_', ' ')}, by id.")
    elif isinstance(field, forms.ModelChoiceField):
        schema = integer(description or f"The {name.replace('_', ' ')}'s id.")
    elif isinstance(field, forms.BooleanField):
        schema = boolean(description)
    elif isinstance(field, (forms.DecimalField, forms.FloatField)):  # both subclass IntegerField
        schema = number(description, **_bounds(field))
    elif isinstance(field, forms.IntegerField):
        schema = integer(description, **_bounds(field))
    elif isinstance(field, forms.DateTimeField):
        schema = string((description.rstrip(".") + ". " if description else "")
                        + "ISO 8601 with an offset, or the event's wall-clock time as YYYY-MM-DDTHH:MM.")
    elif isinstance(field, forms.MultipleChoiceField):
        schema = array(_choice(field), description)
    elif isinstance(field, forms.ChoiceField):
        schema = _choice(field, description)
    elif isinstance(field, forms.EmailField):
        schema = string(description, format="email")
    elif isinstance(field, forms.URLField):
        schema = string(description, format="uri")
    else:
        schema = string(description, **({"maxLength": field.max_length} if getattr(field, "max_length", None) else {}))
    single_choice = isinstance(field, forms.ModelChoiceField) and not isinstance(field, forms.ModelMultipleChoiceField)
    if not field.required and (schema.get("type") in ("integer", "number") or single_choice):
        schema = nullable(schema)  # "no value" may be sent as null
    return schema


def _choice(field, description=""):
    try:
        values = [v for v, _ in field.choices if v not in ("", None)]
    except TypeError:
        values = []
    if values and len(values) <= 30:
        return enum(*values, description=description)
    if values:  # a long list, like time zones: describe it rather than list it
        return string((description + " " if description else "") + f"One of {len(values)} values, e.g. {values[0]!r}.")
    return string(description)


def _bounds(field):
    out = {}
    for validator in field.validators:
        limit = getattr(validator, "limit_value", None)
        if isinstance(limit, (int, float)):
            key = {"MinValueValidator": "minimum", "MaxValueValidator": "maximum"}.get(type(validator).__name__)
            if key:
                out[key] = limit
    return out


# ------------------------------------------------------------ validator

_TYPES = {"string": str, "integer": int, "number": (int, float), "boolean": bool, "object": dict,
          "array": list, "null": type(None)}


def validate(schema, value, where="$", components=None):
    """Every way value breaks schema, as readable lines (empty if none)."""
    components = COMPONENTS if components is None else components
    if "$ref" in schema:
        return validate(components[schema["$ref"][len(REF):]], value, where, components)
    if "anyOf" in schema:
        attempts = [validate(s, value, where, components) for s in schema["anyOf"]]
        if all(attempts):
            return [f"{where}: matches none of the allowed shapes ({'; '.join(a[0] for a in attempts)})"]
        return []
    errors = []
    if "type" in schema:
        kinds = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        if not any(_is(value, k) for k in kinds):
            return [f"{where}: expected {' or '.join(kinds)}, got {type(value).__name__} {str(value)[:40]!r}"]
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{where}: {value!r} isn't one of {schema['enum']}")
    if isinstance(value, str):
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errors.append(f"{where}: longer than {schema['maxLength']}")
        if schema.get("format") == "date-time":
            try:
                datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                errors.append(f"{where}: {value!r} isn't an ISO 8601 date-time")
        if schema.get("format") == "uri" and value and not value.startswith(("http://", "https://", "/")):
            errors.append(f"{where}: {value!r} isn't a URL")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{where}: below {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{where}: above {schema['maximum']}")
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for name in schema.get("required", []):
            if name not in value:
                errors.append(f"{where}: missing {name!r}")
        extra = schema.get("additionalProperties", True)
        for name, item in value.items():
            if name in props:
                errors += validate(props[name], item, f"{where}.{name}", components)
            elif extra is False:
                errors.append(f"{where}: {name!r} isn't documented")
            elif isinstance(extra, dict):
                errors += validate(extra, item, f"{where}.{name}", components)
    if isinstance(value, list) and "items" in schema:
        for i, item in enumerate(value):
            errors += validate(schema["items"], item, f"{where}[{i}]", components)
    return errors


def _is(value, kind):
    if kind == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if kind == "boolean":
        return isinstance(value, bool)
    return isinstance(value, _TYPES[kind])
