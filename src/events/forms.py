from zoneinfo import ZoneInfo, available_timezones

from django import forms
from django.utils.text import slugify

from accounts.models import User, normalize_email

from .models import CustomQuestion, Event, EventRole, Prize, Track

RESERVED_SLUGS = {"new", "manage", "api", "admin"}
DATE_FIELDS = ("starts_at", "submissions_close_at", "judging_ends_at", "results_at")


def timezone_choices():
    zones = sorted(z for z in available_timezones() if "/" in z and not z.startswith(("Etc/", "SystemV/")))
    return [("UTC", "UTC")] + [(z, z.replace("_", " ")) for z in zones]


class LocalDateTimeInput(forms.DateTimeInput):
    input_type = "datetime-local"

    def __init__(self, attrs=None):
        super().__init__(attrs=attrs, format="%Y-%m-%dT%H:%M")


def local_datetime_field(label, required=True, help_text=""):
    return forms.DateTimeField(
        label=label,
        required=required,
        help_text=help_text,
        input_formats=["%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S"],
        widget=LocalDateTimeInput(),
    )


class EventForm(forms.ModelForm):
    """Dates are typed in the event's own time zone and stored in UTC."""

    timezone = forms.ChoiceField(choices=timezone_choices, initial="UTC")
    starts_at = local_datetime_field("Kickoff", help_text="Teams can edit projects from this moment.")
    submissions_close_at = local_datetime_field(
        "Submission deadline", help_text="Hard stop. Nothing can be created or edited after this."
    )
    judging_ends_at = local_datetime_field("Judging ends", required=False)
    results_at = local_datetime_field("Winners announced", required=False)

    class Meta:
        model = Event
        fields = [
            "name", "slug", "tagline", "description", "location", "timezone",
            "starts_at", "submissions_close_at", "judging_ends_at", "results_at",
            "max_team_size", "is_published", "comments_enabled", "gallery_before_deadline",
        ]
        widgets = {"description": forms.Textarea(attrs={"rows": 8})}
        help_texts = {"slug": "Used in the address, e.g. /events/spring-hack-2026/. Leave empty to make one from the name."}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["slug"].required = False
        self.fields["max_team_size"].widget.attrs.update({"min": 1, "max": 10})
        if self.instance.pk:
            tz = self.instance.tzinfo
            for name in DATE_FIELDS:
                value = getattr(self.instance, name)
                if value:
                    self.initial[name] = value.astimezone(tz).replace(tzinfo=None)

    def clean_slug(self):
        slug = slugify(self.cleaned_data.get("slug") or "")
        if not slug:
            return ""
        if slug in RESERVED_SLUGS:
            raise forms.ValidationError("That address is reserved. Pick another.")
        clash = Event.objects.filter(slug=slug).exclude(pk=self.instance.pk)
        if clash.exists():
            raise forms.ValidationError("Another event already uses this address.")
        return slug

    def clean_max_team_size(self):
        size = self.cleaned_data["max_team_size"]
        if not 1 <= size <= 10:
            raise forms.ValidationError("Pick a team size between 1 and 10.")
        return size

    def clean(self):
        cleaned = super().clean()
        tz = ZoneInfo(cleaned.get("timezone") or "UTC")
        for name in DATE_FIELDS:
            value = cleaned.get(name)
            if value:
                # The field parsed the typed wall-clock time as UTC. Re-read
                # that same wall-clock time in the event's zone.
                cleaned[name] = value.replace(tzinfo=None).replace(tzinfo=tz)

        starts, closes = cleaned.get("starts_at"), cleaned.get("submissions_close_at")
        judging, results = cleaned.get("judging_ends_at"), cleaned.get("results_at")
        if starts and closes and closes <= starts:
            self.add_error("submissions_close_at", "The deadline has to be after kickoff.")
        if closes and judging and judging < closes:
            self.add_error("judging_ends_at", "Judging can't end before submissions close.")
        if closes and results and results < closes:
            self.add_error("results_at", "Winners can't be announced before submissions close.")
        if judging and results and results < judging:
            self.add_error("results_at", "Announce winners after judging ends.")

        if not cleaned.get("slug") and cleaned.get("name"):
            cleaned["slug"] = self._unique_slug(cleaned["name"])
        return cleaned

    def _unique_slug(self, name):
        base = slugify(name)[:70] or "hackathon"
        if base in RESERVED_SLUGS:
            base = f"{base}-event"
        slug, n = base, 2
        while Event.objects.filter(slug=slug).exclude(pk=self.instance.pk).exists():
            slug = f"{base}-{n}"
            n += 1
        return slug


class EventCreateForm(EventForm):
    tracks_text = forms.CharField(
        label="Tracks",
        required=False,
        widget=forms.Textarea(attrs={"rows": 4, "placeholder": "Developer tools\nClimate\nHealth"}),
        help_text="One per line. You can add descriptions and prizes after creating the event.",
    )

    def clean_tracks_text(self):
        names, seen = [], set()
        for line in self.cleaned_data.get("tracks_text", "").splitlines():
            name = line.strip()[:80]
            if name and name.lower() not in seen:
                names.append(name)
                seen.add(name.lower())
        if len(names) > 30:
            raise forms.ValidationError("That's a lot of tracks. Keep it to 30 or fewer.")
        return names


class _EventScopedForm(forms.ModelForm):
    def __init__(self, *args, event, **kwargs):
        self.event = event
        super().__init__(*args, **kwargs)
        if "position" in self.fields:
            self.fields["position"].required = False
            self.fields["position"].help_text = "Lower numbers come first. Leave empty to add at the end."

    def next_position(self, model):
        last = model.objects.filter(event=self.event).order_by("-position").values_list("position", flat=True).first()
        return (last or 0) + 1

    def clean_position(self):
        value = self.cleaned_data.get("position")
        if value is None:
            if self.instance.pk:
                return self.instance.position
            return self.next_position(self._meta.model)
        return value


class TrackForm(_EventScopedForm):
    class Meta:
        model = Track
        fields = ["name", "description", "position"]

    def clean_name(self):
        name = self.cleaned_data["name"].strip()
        clash = Track.objects.filter(event=self.event, name__iexact=name).exclude(pk=self.instance.pk)
        if clash.exists():
            raise forms.ValidationError("There's already a track with that name.")
        return name


class PrizeForm(_EventScopedForm):
    class Meta:
        model = Prize
        fields = ["name", "value", "track", "quantity", "description", "position"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["track"].queryset = Track.objects.filter(event=self.event)
        self.fields["track"].empty_label = "Any track (overall prize)"
        self.fields["quantity"].widget.attrs.update({"min": 1, "max": 50})


class QuestionForm(_EventScopedForm):
    class Meta:
        model = CustomQuestion
        fields = ["prompt", "help_text", "kind", "options", "required", "is_public", "position"]
        widgets = {"options": forms.Textarea(attrs={"rows": 3})}

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("kind") == CustomQuestion.Kind.CHOICE:
            options = [o.strip() for o in (cleaned.get("options") or "").splitlines() if o.strip()]
            if len(options) < 2:
                self.add_error("options", "A 'pick one' question needs at least two options, one per line.")
        return cleaned


class OrganizerForm(forms.Form):
    """Add a co-organizer. Judges are added under Judging, with their tracks."""

    email = forms.EmailField(help_text="They need an account first. Ask them to sign up if they haven't.")

    def __init__(self, *args, event, **kwargs):
        self.event = event
        super().__init__(*args, **kwargs)

    def clean(self):
        from teams.models import Membership

        cleaned = super().clean()
        email = cleaned.get("email")
        if not email:
            return cleaned
        user = User.objects.filter(email=normalize_email(email), is_active=True).first()
        if user is None:
            self.add_error("email", "No active account uses that email yet.")
            return cleaned
        if EventRole.objects.filter(event=self.event, user=user, role=EventRole.Role.ORGANIZER).exists():
            self.add_error("email", f"{user.display_name} already organizes this event.")
        if EventRole.objects.filter(event=self.event, user=user, role=EventRole.Role.JUDGE).exists():
            self.add_error(
                "email",
                f"{user.display_name} judges this event. Organizers can see every score, so nobody can be both.",
            )
        if Membership.objects.filter(event=self.event, user=user).exists():
            self.add_error(
                "email",
                f"{user.display_name} is on a team in this event. Staff can't compete in their own event.",
            )
        cleaned["user"] = user
        return cleaned
