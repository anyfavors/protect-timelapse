"""
Pillow-based thumbnail generation.
Produces a 320px-wide JPEG (aspect-ratio preserved) at quality=60.
Used by the capture worker and historical extraction pipeline.
"""

import io

from PIL import Image

THUMBNAIL_WIDTH = 320
THUMBNAIL_QUALITY = 60


def generate_thumbnail(image_bytes: bytes) -> bytes:
    """
    Resize *image_bytes* (JPEG) to THUMBNAIL_WIDTH px wide (aspect-ratio
    preserved) and return the result as a JPEG byte string.
    """
    src = Image.open(io.BytesIO(image_bytes))
    orig_w, orig_h = src.size
    if orig_w == 0 or orig_h == 0:
        raise ValueError(
            f"Invalid image dimensions: {orig_w}x{orig_h}"
        )  # prevents ZeroDivisionError (#15)
    new_h = max(1, int(orig_h * THUMBNAIL_WIDTH / orig_w))
    img = src.resize((THUMBNAIL_WIDTH, new_h), Image.Resampling.LANCZOS)

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=THUMBNAIL_QUALITY, optimize=True)
    return buf.getvalue()


def generate_thumbnail_from_pillow(img: Image.Image) -> bytes:
    """
    Same as generate_thumbnail() but accepts an already-opened Pillow Image.
    Used when the image is already in memory from a luminance check to avoid
    a second decode.
    """
    orig_w, orig_h = img.size
    if orig_w == 0 or orig_h == 0:
        raise ValueError(
            f"Invalid image dimensions: {orig_w}x{orig_h}"
        )  # prevents ZeroDivisionError (#15)
    new_h = max(1, int(orig_h * THUMBNAIL_WIDTH / orig_w))
    resized = img.resize((THUMBNAIL_WIDTH, new_h), Image.Resampling.LANCZOS)

    buf = io.BytesIO()
    resized.save(buf, format="JPEG", quality=THUMBNAIL_QUALITY, optimize=True)
    return buf.getvalue()
