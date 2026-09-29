from django import forms
from django.utils.text import slugify

from accounts.models import User, normalize_email
from events.models import EventRole, Track
from projects.models import Project

from .models import Criterion, JudgingConfig


class ConfigForm(forms.ModelForm):
    class Meta:
        model = JudgingConfig
        fields = ["scale_min", "scale_max", "reviews_per_project", "kappa"]

    def __init__(self, *args, locked=False, **kwargs):
        super().__init__(*args, **kwargs)
        for name in ("scale_min", "scale_max"):
            self.fields[name].widget.attrs.update({"min": 0, "max": 100})
            if locked:
                self.fields[name].disabled = True
                self.fields[name].help_text = "Fixed once the first score is in, so every score shares one scale."
        self.fields["reviews_per_project"].widget.attrs.update({"min": 1, "max": 10})
        self.fields["kappa"].widget.attrs.update({"min": 0, "max": 100, "step": "0.5"})

    def clean(self):
        cleaned = super().clean()
        low, high = cleaned.get("scale_min"), cleaned.get("scale_max")
        if low is not None and high is not None:
            if high <= low:
                self.add_error("scale_max", "The highest mark has to be above the lowest.")
            elif high - low > 20:
                self.add_error("scale_max", "Keep the scale to 20 steps or fewer; nobody can tell a 37 from a 38.")
        reviews = cleaned.get("reviews_per_project")
        if reviews is not None and not 1 <= reviews <= 10:
            self.add_error("reviews_per_project", "Between 1 and 10 reviews per project.")
        kappa = cleaned.get("kappa")
        if kappa is not None and not 0 <= kappa <= 100:
            self.add_error("kappa", "Keep kappa between 0 and 100.")
        return cleaned


class CriterionForm(forms.ModelForm):
    class Meta:
        model = Criterion
        fields = ["label", "key", "description", "weight", "position"]

    def __init__(self, *args, event, locked=False, **kwargs):
        self.event = event
        super().__init__(*args, **kwargs)
        self.fields["key"].required = False
        self.fields["key"].help_text = "Leave empty to make one from the label."
        self.fields["position"].required = False
        self.fields["position"].help_text = "Order on the scoring form. Leave empty to add at the end."
        self.fields["weight"].widget.attrs.update({"min": "0.01", "max": "100", "step": "0.25"})
        if locked and self.instance.pk:
            self.fields["key"].disabled = True

    def clean_weight(self):
        weight = self.cleaned_data["weight"]
        if weight is None or weight <= 0:
            raise forms.ValidationError("Weights have to be above zero.")
        if weight > 100:
            raise forms.ValidationError("Keep weights at 100 or below.")
        return weight

    def clean(self):
        cleaned = super().clean()
        key = slugify(cleaned.get("key") or cleaned.get("label") or "")[:40]
        if not key and not self.errors:
            self.add_error("label", "Give the criterion a name.")
            return cleaned
        clash = Criterion.objects.filter(event=self.event, key=key).exclude(pk=self.instance.pk)
        if clash.exists():
            self.add_error("key", "Another criterion already uses that short name.")
        cleaned["key"] = key
        if cleaned.get("position") is None:
            if self.instance.pk:
                cleaned["position"] = self.instance.position
            else:
                last = Criterion.objects.filter(event=self.event).order_by("-position").values_list(
                    "position", flat=True).first()
                cleaned["position"] = (last or 0) + 1
        return cleaned


def _track_field(event, label="Tracks"):
    return forms.ModelMultipleChoiceField(
        queryset=Track.objects.filter(event=event), required=False, widget=forms.CheckboxSelectMultiple,
        label=label, help_text="They'll only ever be given projects from these tracks.",
    )


class JudgeAddForm(forms.Form):
    email = forms.EmailField(help_text="Someone who already has an account. For anyone else, make an invite link.")
    all_tracks = forms.BooleanField(required=False, label="Floater: can judge every track")

    def __init__(self, *args, event, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["tracks"] = _track_field(event)
        self.order_fields(["email", "tracks", "all_tracks"])

    def clean_email(self):
        user = User.objects.filter(email=normalize_email(self.cleaned_data["email"])).first()
        if user is None:
            raise forms.ValidationError("No account uses that email. Send them an invite link instead.")
        self.user = user
        return user.email


class JudgeInviteForm(forms.Form):
    email = forms.EmailField(required=False, help_text="Optional. If given, only that account can use the link.")
    all_tracks = forms.BooleanField(required=False, label="Floater: can judge every track")
    days = forms.IntegerField(min_value=1, max_value=60, initial=14, label="Link works for (days)")

    def __init__(self, *args, event, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["tracks"] = _track_field(event)
        self.order_fields(["email", "tracks", "all_tracks", "days"])


class JudgeTracksForm(forms.Form):
    all_tracks = forms.BooleanField(required=False, label="Floater: can judge every track")

    def __init__(self, *args, event, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["tracks"] = _track_field(event)
        self.order_fields(["tracks", "all_tracks"])


class JudgeChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, user):
        return f"{user.display_name} ({user.email})"


class ProjectChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, project):
        return f"{project.title} · {project.track.name if project.track else 'no track'} · #{project.pk}"


class ProjectMultipleField(forms.ModelMultipleChoiceField):
    def label_from_instance(self, project):
        return f"{project.title} ({project.track.name if project.track else 'no track'})"


class JudgeMultipleField(forms.ModelMultipleChoiceField):
    def label_from_instance(self, user):
        return user.display_name


def judge_users(event):
    return User.objects.filter(event_roles__event=event, event_roles__role=EventRole.Role.JUDGE).order_by("name")


def listed(event):
    return (Project.objects.filter(event=event, status=Project.Status.SUBMITTED, duplicate_of__isnull=True)
            .select_related("track").order_by("track__position", "title"))


class ConflictForm(forms.Form):
    reason = forms.CharField(max_length=200, help_text="For example: works at the same company as the team.")

    def __init__(self, *args, event, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["judge"] = JudgeChoiceField(queryset=judge_users(event))
        self.fields["project"] = ProjectChoiceField(queryset=listed(event))
        self.order_fields(["judge", "project", "reason"])


class ManualAssignForm(forms.Form):
    def __init__(self, *args, event, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["judge"] = JudgeChoiceField(queryset=judge_users(event))
        self.fields["project"] = ProjectChoiceField(queryset=listed(event))


class BatchForm(forms.Form):
    SCOPES = [
        ("gaps", "Every project below the target"),
        ("track", "Projects in one track"),
        ("chosen", "Projects I pick"),
    ]
    JUDGE_SCOPES = [("all", "Every judge"), ("chosen", "Judges I pick")]

    label = forms.CharField(max_length=80)
    scope = forms.ChoiceField(choices=SCOPES, widget=forms.RadioSelect, initial="gaps", label="Which projects")
    judges_scope = forms.ChoiceField(choices=JUDGE_SCOPES, widget=forms.RadioSelect, initial="all",
                                     label="Which judges")
    target = forms.IntegerField(min_value=1, max_value=10, label="Reviews per project",
                                help_text="Projects are topped up to this. Existing reviews count.")
    seed = forms.IntegerField(required=False, min_value=1, max_value=10**12,
                              help_text="Leave empty for a fresh random one. It's recorded, so any batch can be reproduced.")

    def __init__(self, *args, event, **kwargs):
        super().__init__(*args, **kwargs)
        self.event = event
        self.fields["track"] = forms.ModelChoiceField(queryset=Track.objects.filter(event=event), required=False,
                                                      empty_label="Pick a track")
        self.fields["projects"] = ProjectMultipleField(queryset=listed(event), required=False,
                                                       widget=forms.CheckboxSelectMultiple)
        self.fields["judges"] = JudgeMultipleField(queryset=judge_users(event), required=False,
                                                   widget=forms.CheckboxSelectMultiple)
        self.order_fields(["label", "scope", "track", "projects", "judges_scope", "judges", "target", "seed"])

    def clean(self):
        cleaned = super().clean()
        scope = cleaned.get("scope")
        if scope == "track" and not cleaned.get("track"):
            self.add_error("track", "Pick the track.")
        if scope == "chosen" and not cleaned.get("projects"):
            self.add_error("projects", "Tick at least one project.")
        if cleaned.get("judges_scope") == "chosen" and not cleaned.get("judges"):
            self.add_error("judges", "Tick at least one judge.")
        return cleaned

    def scope_params(self):
        c = self.cleaned_data
        params = {}
        if c["scope"] == "track":
            params["track"] = c["track"]
            text = f"track {c['track'].name}"
        elif c["scope"] == "chosen":
            params["project_ids"] = [p.pk for p in c["projects"]]
            text = f"{len(params['project_ids'])} chosen project{'s' if len(params['project_ids']) != 1 else ''}"
        else:
            text = "every project below target"
        if c["judges_scope"] == "chosen":
            params["judge_ids"] = [u.pk for u in c["judges"]]
            text += f", {len(params['judge_ids'])} chosen judge{'s' if len(params['judge_ids']) != 1 else ''}"
        else:
            text += ", every judge"
        return params, f"{text}, up to {c['target']} review{'s' if c['target'] != 1 else ''} each"
