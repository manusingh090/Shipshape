from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
from django.db import models
from django.utils import timezone


def normalize_email(email):
    return (email or "").strip().lower()


class UserManager(BaseUserManager):
    use_in_migrations = True

    def create_user(self, email, name="", password=None, **extra):
        email = normalize_email(email)
        if not email:
            raise ValueError("An email address is required.")
        user = self.model(email=email, name=name.strip(), **extra)
        if password:
            user.set_password(password)
        else:
            user.set_unusable_password()
        user.save(using=self._db)
        return user

    def create_superuser(self, email, name="", password=None, **extra):
        extra.setdefault("platform_role", User.Role.ADMIN)
        return self.create_user(email, name, password, **extra)

    def get_by_natural_key(self, email):
        return self.get(email=normalize_email(email))


class User(AbstractBaseUser):
    """An account. The platform role says what someone may do site-wide.

    Per-event roles (organizer, judge) live in events.EventRole, and being a
    participant means being on a team in that event. A visitor is simply
    someone with no session.
    """

    class Role(models.TextChoices):
        MEMBER = "member", "Member"
        ORGANIZER = "organizer", "Organizer"
        ADMIN = "admin", "Admin"

    email = models.EmailField(unique=True)
    name = models.CharField(max_length=120)
    platform_role = models.CharField(max_length=16, choices=Role.choices, default=Role.MEMBER)
    is_active = models.BooleanField(default=True)
    date_joined = models.DateTimeField(default=timezone.now)
    # Where an account came from, e.g. "jdg_24" for a judge in fixtures.json.
    external_id = models.CharField(max_length=40, blank=True, default="", db_index=True)

    USERNAME_FIELD = "email"
    EMAIL_FIELD = "email"
    REQUIRED_FIELDS = ["name"]

    objects = UserManager()

    class Meta:
        ordering = ["name", "email"]

    def __str__(self):
        return self.name or self.email

    def save(self, *args, **kwargs):
        self.email = normalize_email(self.email)
        super().save(*args, **kwargs)

    @property
    def is_admin(self):
        return self.is_active and self.platform_role == self.Role.ADMIN

    @property
    def can_create_events(self):
        return self.is_active and self.platform_role in (self.Role.ORGANIZER, self.Role.ADMIN)

    @property
    def display_name(self):
        return self.name or self.email.split("@")[0]

    @property
    def short_name(self):
        return self.display_name.split()[0]

    @property
    def initials(self):
        parts = [p for p in self.display_name.replace(".", " ").split() if p[:1].isalpha()]
        if not parts:
            return self.display_name[:1].upper()
        if len(parts) == 1:
            return parts[0][:2].upper()
        return (parts[0][0] + parts[-1][0]).upper()


class UserSession(models.Model):
    """A signed-in browser. Mirrors a row in django_session so people can see
    where they are signed in and end sessions they do not recognise."""

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="browser_sessions")
    session_key = models.CharField(max_length=40, unique=True)
    created_at = models.DateTimeField(default=timezone.now)
    last_seen_at = models.DateTimeField(default=timezone.now)
    user_agent = models.CharField(max_length=300, blank=True)
    ip = models.GenericIPAddressField(null=True, blank=True)
    label = models.CharField(max_length=80, blank=True)

    class Meta:
        ordering = ["-last_seen_at"]

    def __str__(self):
        return f"{self.user} @ {self.ip or 'unknown'}"

    @property
    def device(self):
        if self.label:
            return self.label
        return describe_user_agent(self.user_agent)


class LoginAttempt(models.Model):
    email = models.CharField(max_length=254, db_index=True)
    ip = models.GenericIPAddressField(null=True, blank=True)
    succeeded = models.BooleanField(default=False)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)


def describe_user_agent(ua):
    """Good enough to recognise your own laptop. Not a full UA parser."""
    ua = ua or ""
    if not ua:
        return "Unknown device"
    browser = "Browser"
    for needle, name in (("Edg/", "Edge"), ("OPR/", "Opera"), ("Firefox/", "Firefox"),
                         ("Chrome/", "Chrome"), ("Safari/", "Safari"), ("curl/", "curl"),
                         ("Python-urllib", "Python script")):
        if needle in ua:
            browser = name
            break
    system = ""
    for needle, name in (("Windows", "Windows"), ("Android", "Android"), ("iPhone", "iPhone"),
                         ("iPad", "iPad"), ("Mac OS X", "macOS"), ("Linux", "Linux")):
        if needle in ua:
            system = name
            break
    return f"{browser} on {system}" if system else browser
