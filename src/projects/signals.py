from django.db import transaction
from django.db.models.signals import post_delete
from django.dispatch import receiver

from .images import delete_media
from .models import Project, ProjectImage


@receiver(post_delete, sender=ProjectImage)
def remove_image_file(sender, instance, **kwargs):
    path = instance.path
    transaction.on_commit(lambda: delete_media(path))


@receiver(post_delete, sender=Project)
def remove_thumbnail_file(sender, instance, **kwargs):
    path = instance.thumbnail
    if path:
        transaction.on_commit(lambda: delete_media(path))
