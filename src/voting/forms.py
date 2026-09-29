from django import forms

from events.forms import local_datetime_field

from .models import VotingConfig

LOCKED_HELP = "Fixed once the first ballot is in, so every ballot in the count was cast under the same rules."


class VotingConfigForm(forms.ModelForm):
    """Dates are typed in the event's time zone and stored in UTC, like the
    event's own dates."""

    opens_at = local_datetime_field(
        "Voting opens", required=False,
        help_text="Leave empty to open when submissions close, so everyone votes on final projects.",
    )
    closes_at = local_datetime_field("Voting closes", required=False)

    class Meta:
        model = VotingConfig
        fields = ["is_enabled", "access", "method", "credits", "max_votes", "order", "opens_at", "closes_at",
                  "email_domains"]
        widgets = {"access": forms.RadioSelect, "method": forms.RadioSelect, "order": forms.RadioSelect}

    def __init__(self, *args, event, locked=False, **kwargs):
        self.event = event
        super().__init__(*args, **kwargs)
        self.fields["access"].help_text = (
            "Open link: anyone you give the link to, no account. Email-gated: a one-time link sent to their address. "
            "Signed in: a Shipshape account."
        )
        self.fields["credits"].widget.attrs.update({"min": 4, "max": 400})
        self.fields["max_votes"].widget.attrs.update({"min": 1, "max": 20})
        if locked:
            for name in ("access", "method", "credits", "max_votes", "order"):
                self.fields[name].disabled = True
                self.fields[name].help_text = LOCKED_HELP
        tz = event.tzinfo
        for name in ("opens_at", "closes_at"):
            value = getattr(self.instance, name)
            if value:
                self.initial[name] = value.astimezone(tz).replace(tzinfo=None)

    def clean_credits(self):
        credits = self.cleaned_data["credits"]
        if credits is None or not 4 <= credits <= 400:
            raise forms.ValidationError("Between 4 and 400 credits.")
        return credits

    def clean_max_votes(self):
        cap = self.cleaned_data["max_votes"]
        if cap is None or not 1 <= cap <= 20:
            raise forms.ValidationError("Between 1 and 20 votes.")
        return cap

    def clean_email_domains(self):
        raw = self.cleaned_data.get("email_domains", "")
        domains = [d.strip().lower().lstrip("@") for d in raw.split(",") if d.strip()]
        for domain in domains:
            if "." not in domain or " " in domain or "@" in domain:
                raise forms.ValidationError(f"“{domain}” isn't a domain. Write them like example.org, uni.edu.")
        return ", ".join(domains)

    def clean(self):
        cleaned = super().clean()
        tz = self.event.tzinfo
        for name in ("opens_at", "closes_at"):
            value = cleaned.get(name)
            if value:
                # Parsed as UTC; re-read the same wall-clock time in the event's zone.
                cleaned[name] = value.replace(tzinfo=None).replace(tzinfo=tz)
        opens, closes = cleaned.get("opens_at"), cleaned.get("closes_at")
        start = opens or self.event.submissions_close_at
        if opens and opens < self.event.submissions_close_at:
            self.add_error("opens_at", "Voting can't open before submissions close: projects could still change.")
        if closes and closes <= start:
            self.add_error("closes_at", "Voting has to close after it opens.")
        if cleaned.get("is_enabled") and not closes:
            self.add_error("closes_at", "Set a closing time. A vote that never closes never has a result.")
        return cleaned
