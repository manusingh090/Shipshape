import re

from django import forms
from django.core.validators import URLValidator

from events.models import CustomQuestion

web_url = URLValidator(schemes=["http", "https"])
TAG_PATTERN = re.compile(r"^[a-z0-9.][a-z0-9 .+#_-]{0,39}$")
MAX_TAGS = 12


def url_field(label, help_text=""):
    return forms.URLField(
        label=label, required=False, max_length=500, assume_scheme="https",
        validators=[web_url], help_text=help_text,
    )


def parse_tags(raw):
    tags = []
    for piece in (raw or "").split(","):
        tag = " ".join(piece.strip().lower().split())
        if not tag:
            continue
        if len(tag) > 40:
            raise forms.ValidationError(f"“{tag[:24]}…” is too long for a tag (40 characters at most).")
        if not TAG_PATTERN.match(tag):
            raise forms.ValidationError(
                f"Tags can use letters, numbers, spaces and . + # _ -. “{tag}” has something else in it."
            )
        if tag not in tags:
            tags.append(tag)
    if len(tags) > MAX_TAGS:
        raise forms.ValidationError(f"Up to {MAX_TAGS} tags, please.")
    return tags


def question_field(question):
    common = {"label": question.prompt, "required": False, "help_text": question.help_text}
    kind = question.kind
    if kind == CustomQuestion.Kind.LONG:
        return forms.CharField(max_length=5000, widget=forms.Textarea(attrs={"rows": 4}), **common)
    if kind == CustomQuestion.Kind.URL:
        return forms.URLField(max_length=500, assume_scheme="https", validators=[web_url], **common)
    if kind == CustomQuestion.Kind.CHOICE:
        choices = [("", "Choose one")] + [(o, o) for o in question.option_list]
        return forms.ChoiceField(choices=choices, **common)
    if kind == CustomQuestion.Kind.YESNO:
        return forms.ChoiceField(choices=[("", "Choose"), ("yes", "Yes"), ("no", "No")], **common)
    return forms.CharField(max_length=300, **common)


class ProjectForm(forms.Form):
    """The submission form. Used by the web page and by the JSON API.

    Drafts only need a name. `enforce_required` switches on the rules for a
    submitted project: tagline, description, track and every required
    organizer question. It is on when submitting and when editing something
    already submitted, so a live entry can't be emptied out by accident.
    """

    title = forms.CharField(max_length=120, label="Project name")
    tagline = forms.CharField(
        max_length=200, required=False, label="Tagline",
        help_text="One sentence. It's what people read on the gallery card.",
    )
    track = forms.ModelChoiceField(queryset=None, required=False, empty_label="Choose a track")
    description = forms.CharField(
        max_length=20000, required=False, label="Description",
        widget=forms.Textarea(attrs={"rows": 14}),
        help_text="What it does, how you built it, what you'd do next. Markdown works: **bold**, _italic_, lists, links.",
    )
    repo_url = url_field("Source code", "Link to the repository.")
    live_url = url_field("Live link", "Where people can try it, if it's hosted somewhere.")
    demo_video_url = url_field("Demo video", "A link to your video on any video site. Two or three minutes is plenty.")
    tags = forms.CharField(
        max_length=500, required=False, label="Tech tags",
        help_text="Comma separated, up to 12. For example: django, sqlite, raspberry pi",
    )
    thumbnail = forms.FileField(
        required=False, label="Thumbnail",
        help_text="JPEG, PNG, WebP or GIF, up to 5 MB. Shown on your gallery card; landscape works best.",
        widget=forms.FileInput(attrs={"accept": "image/jpeg,image/png,image/webp,image/gif"}),
    )
    remove_thumbnail = forms.BooleanField(required=False, label="Remove the current thumbnail")

    def __init__(self, *args, event, project=None, enforce_required=False, **kwargs):
        self.event = event
        self.project = project
        self.enforce_required = enforce_required
        super().__init__(*args, **kwargs)
        self.fields["track"].queryset = event.tracks.all()
        for name in ("tagline", "track", "description"):
            self.fields[name].needed_to_submit = True  # read by _field.html
        self.questions = list(event.questions.all())
        answers = {}
        if project is not None and project.pk:
            answers = {a.question_id: a.value for a in project.answers.all()}
            if not self.is_bound:
                self.initial.update({
                    "title": project.title,
                    "tagline": project.tagline,
                    "track": project.track_id,
                    "description": project.description,
                    "repo_url": project.repo_url,
                    "live_url": project.live_url,
                    "demo_video_url": project.demo_video_url,
                    "tags": ", ".join(project.tags.values_list("name", flat=True)),
                })
        for question in self.questions:
            key = self.question_key(question)
            self.fields[key] = question_field(question)
            if not self.is_bound:
                self.initial.setdefault(key, answers.get(question.pk, ""))

    @staticmethod
    def question_key(question):
        return f"q_{question.pk}"

    def question_fields(self):
        return [(q, self[self.question_key(q)]) for q in self.questions]

    def clean_title(self):
        title = self.cleaned_data["title"].strip()
        if not title:
            raise forms.ValidationError("Your project needs a name.")
        return title

    def clean_tags(self):
        return parse_tags(self.cleaned_data.get("tags"))

    def clean(self):
        cleaned = super().clean()
        if self.enforce_required:
            checks = (
                ("tagline", "Add a one-line tagline before submitting."),
                ("description", "Describe the project before submitting."),
                ("track", "Pick a track before submitting."),
            )
            for name, message in checks:
                value = cleaned.get(name)
                if (value is None or (isinstance(value, str) and not value.strip())) and name not in self.errors:
                    self.add_error(name, message)
            for question in self.questions:
                key = self.question_key(question)
                if question.required and not str(cleaned.get(key) or "").strip() and key not in self.errors:
                    self.add_error(key, "The organizers need an answer to this before you submit.")
        return cleaned
