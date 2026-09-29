"""Image uploads.

Every upload is opened with Pillow, checked, and re-encoded to a fresh JPEG
(or PNG when it has transparency). Re-encoding throws away EXIF data,
including GPS coordinates, and means we only ever serve bytes we wrote
ourselves: an HTML or SVG file renamed to .png never makes it to disk.
"""

import uuid
import warnings
from pathlib import Path

from django.conf import settings
from PIL import Image, ImageOps, UnidentifiedImageError

ACCEPTED_FORMATS = {"JPEG", "PNG", "WEBP", "GIF"}
MAX_EDGE = 1600
Image.MAX_IMAGE_PIXELS = 40_000_000


class ImageError(Exception):
    pass


def media_root():
    return Path(settings.MEDIA_ROOT)


def process_upload(upload, folder):
    """Validate and store one uploaded image. Returns (relative_path, width, height)."""
    name = getattr(upload, "name", "file")
    if upload.size > settings.MAX_IMAGE_BYTES:
        mb = settings.MAX_IMAGE_BYTES // (1024 * 1024)
        raise ImageError(f"{name} is larger than {mb} MB.")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            upload.seek(0)
            probe = Image.open(upload)
            fmt = probe.format
            probe.verify()
            upload.seek(0)
            image = Image.open(upload)
            image.load()
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError,
            Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise ImageError(f"{name} isn't an image we can read. Use JPEG, PNG, WebP or GIF.")
    if fmt not in ACCEPTED_FORMATS:
        raise ImageError(f"{name} is a {fmt or 'unknown'} file. Use JPEG, PNG, WebP or GIF.")

    image = ImageOps.exif_transpose(image)
    has_alpha = False
    if image.mode in ("RGBA", "LA", "PA") or (image.mode == "P" and "transparency" in image.info):
        image = image.convert("RGBA")
        # Screenshots often carry an alpha channel that is fully opaque;
        # those are smaller as JPEG and lose nothing.
        has_alpha = image.getchannel("A").getextrema()[0] < 255
    image = image.convert("RGBA" if has_alpha else "RGB")
    image.thumbnail((MAX_EDGE, MAX_EDGE))

    ext = "png" if has_alpha else "jpg"
    relative = f"{folder}/{uuid.uuid4().hex}.{ext}"
    target = media_root() / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    if has_alpha:
        image.save(target, "PNG", optimize=True)
    else:
        image.save(target, "JPEG", quality=85, optimize=True, progressive=True)
    return relative, image.width, image.height


def delete_media(relative):
    if not relative:
        return
    root = media_root().resolve()
    target = (root / relative).resolve()
    if root in target.parents and target.is_file():
        try:
            target.unlink()
        except OSError:
            pass
