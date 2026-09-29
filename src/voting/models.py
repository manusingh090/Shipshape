"""Community voting data.

One ballot per voter per event. Who counts as "one voter" depends on the
access mode the organizer picked:

* open link: one ballot per browser session that opened the secret link;
* email-gated: one ballot per confirmed email address;
* signed in: one ballot per account.

Ballots can be changed until voting closes; a save replaces the ballot's
entries. Nothing derived (totals, rankings) is stored: voting/results.py
counts from the entries on every request.
"""

import hashlib
import secrets
from datetime import timedelta

from django.conf import settings
from django.db import models
from django.db.models import F, Q
from django.utils import timezone


def new_token():
    return secrets.token_urlsafe(24)


def hash_token(token):
    return hashlib.sha256(token.encode()).hexdigest()


class VotingConfig(models.Model):
    """Per-event community vote settings. Created when an organizer first
    opens the voting page."""

    class Access(models.TextChoices):
        LINK = "link", "Open link"
        EMAIL = "email", "Email-gated"
        ACCOUNT = "account", "Signed in"

    class Method(models.TextChoices):
        QUADRATIC = "quadratic", "Quadratic voting"
        SINGLE = "single", "One person, one vote"

    event = models.OneToOneField("events.Event", on_delete=models.CASCADE, related_name="voting_config")
    is_enabled = models.BooleanField(
        "run a community vote", default=False,
        help_text="Off means no ballot page, and the event page doesn't mention voting.",
    )
    access = models.CharField(
        "who can vote", max_length=10, choices=Access.choices, default=Access.ACCOUNT,
    )
    class Order(models.TextChoices):
        SHUFFLED = "shuffled", "Shuffled for each voter"
        ALPHABETICAL = "alphabetical", "A to Z"

    method = models.CharField(max_length=10, choices=Method.choices, default=Method.QUADRATIC)
    order = models.CharField(
        "ballot order", max_length=12, choices=Order.choices, default=Order.SHUFFLED,
        help_text="Shuffled gives every voter their own order, the same each time they come back, so no project "
                  "gains from sitting at the top. A to Z hands the first few projects most of the votes.",
    )
    credits = models.PositiveSmallIntegerField(
        "credits per voter", default=25,
        help_text="Quadratic only. n votes on one project cost n × n credits.",
    )
    max_votes = models.PositiveSmallIntegerField(
        "most votes on one project", default=3,
        help_text="Quadratic only. Stops a group of friends going all-in on one project. 3 is the tested default.",
    )
    opens_at = models.DateTimeField(
        "voting opens", null=True, blank=True,
        help_text="Leave empty to open when submissions close, so everyone votes on final projects.",
    )
    closes_at = models.DateTimeField("voting closes", null=True, blank=True)
    link_token = models.CharField(max_length=48, unique=True, default=new_token)
    email_domains = models.CharField(
        "only these email domains", max_length=300, blank=True,
        help_text="Email-gated only. Comma separated, e.g. example.org, uni.edu. Leave empty to allow any address.",
    )
    # Results stay with the organizers until voting has closed AND an organizer
    # has looked over the ballots and published them (Devpost's advice: don't
    # show results live, review the votes first).
    results_published_at = models.DateTimeField(null=True, blank=True)
    results_published_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(credits__gte=1), name="voting_credits_positive"),
            models.CheckConstraint(condition=Q(max_votes__gte=1), name="voting_max_votes_positive"),
            models.CheckConstraint(
                condition=Q(opens_at__isnull=True) | Q(closes_at__isnull=True) | Q(closes_at__gt=F("opens_at")),
                name="voting_closes_after_it_opens",
            ),
        ]

    def __str__(self):
        return f"Community vote for {self.event}"

    @property
    def is_quadratic(self):
        return self.method == self.Method.QUADRATIC

    @property
    def domain_list(self):
        return [d.strip().lower().lstrip("@") for d in self.email_domains.split(",") if d.strip()]


class Ballot(models.Model):
    class Kind(models.TextChoices):
        LINK = "link", "Open link"
        EMAIL = "email", "Email"
        ACCOUNT = "account", "Account"

    event = models.ForeignKey("events.Event", on_delete=models.CASCADE, related_name="ballots")
    # Names the ballot in the voter's session. Never shown to anyone else.
    key = models.CharField(max_length=48, unique=True, default=new_token)
    kind = models.CharField(max_length=10, choices=Kind.choices)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="ballots"
    )
    email = models.EmailField(blank=True)
    # Kept so organizers can look into a suspicious burst of ballots later.
    # The address is hashed with the site's secret, never stored raw.
    ip_hash = models.CharField(max_length=64, blank=True)
    user_agent = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(default=timezone.now)
    # An organizer who reviewed the ballots can take one out of the count, with
    # a reason. The ballot stays, so the decision is on record and reversible.
    # The voter isn't told.
    excluded_at = models.DateTimeField(null=True, blank=True)
    excluded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    excluded_reason = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["created_at", "id"]
        constraints = [
            models.UniqueConstraint(fields=["event", "user"], condition=Q(user__isnull=False),
                                    name="one_ballot_per_account"),
            models.UniqueConstraint(fields=["event", "email"], condition=~Q(email=""),
                                    name="one_ballot_per_email"),
        ]

    def __str__(self):
        return f"Ballot {self.pk} in {self.event}"


class BallotEntry(models.Model):
    ballot = models.ForeignKey(Ballot, on_delete=models.CASCADE, related_name="entries")
    project = models.ForeignKey("projects.Project", on_delete=models.CASCADE, related_name="ballot_entries")
    votes = models.PositiveSmallIntegerField()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["ballot", "project"], name="one_entry_per_project_per_ballot"),
            models.CheckConstraint(condition=Q(votes__gte=1), name="ballot_entry_has_votes"),
        ]

    def __str__(self):
        return f"{self.votes} for {self.project_id}"


def default_pass_expiry():
    return timezone.now() + timedelta(minutes=30)


class EmailPass(models.Model):
    """A one-time link that proves someone can read mail at an address.

    Only the hash of the token is stored, the way a password reset link
    should be: a copy of the database doesn't let anyone vote as someone else.
    """

    event = models.ForeignKey("events.Event", on_delete=models.CASCADE, related_name="+")
    email = models.EmailField()
    token_hash = models.CharField(max_length=64, unique=True)
    ip_hash = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    expires_at = models.DateTimeField(default=default_pass_expiry)
    used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Email pass for {self.email}"
