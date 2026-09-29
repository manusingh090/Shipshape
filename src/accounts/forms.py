from django import forms
from django.contrib.auth.password_validation import validate_password

from .models import User, normalize_email


class LoginForm(forms.Form):
    email = forms.EmailField(
        max_length=254,
        widget=forms.EmailInput(attrs={"autocomplete": "email", "autofocus": True}),
    )
    password = forms.CharField(
        strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "current-password"}),
    )


class SignupForm(forms.Form):
    name = forms.CharField(
        max_length=120,
        label="Your name",
        help_text="Shown on your team and your projects.",
        widget=forms.TextInput(attrs={"autocomplete": "name", "autofocus": True}),
    )
    email = forms.EmailField(max_length=254, widget=forms.EmailInput(attrs={"autocomplete": "email"}))
    password1 = forms.CharField(
        label="Password",
        strip=False,
        help_text="At least 10 characters. A short sentence works well.",
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
    )
    password2 = forms.CharField(
        label="Password, again",
        strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
    )

    def clean_name(self):
        name = self.cleaned_data["name"].strip()
        if not name:
            raise forms.ValidationError("Tell us what to call you.")
        return name

    def clean_email(self):
        email = normalize_email(self.cleaned_data["email"])
        if User.objects.filter(email=email).exists():
            raise forms.ValidationError("There is already an account with this email. Try signing in.")
        return email

    def clean(self):
        cleaned = super().clean()
        p1, p2 = cleaned.get("password1"), cleaned.get("password2")
        if p1 and p2 and p1 != p2:
            self.add_error("password2", "The two passwords don't match.")
        if p1:
            probe = User(email=cleaned.get("email", ""), name=cleaned.get("name", ""))
            try:
                validate_password(p1, user=probe)
            except forms.ValidationError as exc:
                self.add_error("password1", exc)
        return cleaned

    def save(self):
        return User.objects.create_user(
            email=self.cleaned_data["email"],
            name=self.cleaned_data["name"],
            password=self.cleaned_data["password1"],
        )


class ProfileForm(forms.ModelForm):
    class Meta:
        model = User
        fields = ["name"]
        labels = {"name": "Your name"}
