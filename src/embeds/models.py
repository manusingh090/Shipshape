from django.db import models


class EmbedSettings(models.Model):
    """Whether an event's gallery may be embedded on other sites, and where.

    Embedding is on by default, because the gallery is public anyway: the
    widget shows exactly what a signed-out visitor sees, and it has no forms,
    so there's nothing to click-jack.
    """

    event = models.OneToOneField("events.Event", on_delete=models.CASCADE, related_name="embed_settings")
    enabled = models.BooleanField("let other sites embed the gallery", default=True)
    allowed_origins = models.TextField(
        "only on these sites", blank=True,
        help_text="One per line, like https://hackathon.example.org. Leave empty to allow any site.",
    )
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Embedding for {self.event}"

    @property
    def origin_list(self):
        return [line.strip().rstrip("/") for line in self.allowed_origins.splitlines() if line.strip()]
