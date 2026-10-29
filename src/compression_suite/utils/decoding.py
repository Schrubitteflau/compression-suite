"""Decode an image to the end, to tell a damaged file (truncated, cut by an interrupted sync) from a sound one.

A file is damaged when its format is recognised but its decoder can't read it to the end. Only the formats a pinned
decoder reads are checked: Pillow for JPEG, PNG, WebP, GIF and AVIF, pillow-heif for HEIC and HEIF (called directly,
not registered as a Pillow plugin). Any other format, PDF included, is never said to be damaged.
"""

import io
from pathlib import Path

import pillow_heif
from PIL import Image

PILLOW_FORMATS = ("jpeg", "png", "webp", "gif", "avif")
HEIF_FORMATS = ("heic", "heif")

# What the decoders raise on a file they can't read (Pillow raises SyntaxError for a truncated AVIF, for instance)
DECODING_ERRORS = (OSError, SyntaxError, ValueError, EOFError)


class CorruptImageError(Exception):
    """A file whose format is recognised but that its decoder can't read to the end; the message is the decoder's."""


def _open(source: Path | bytes) -> Path | io.BytesIO:
    """What the decoders open: the path of a file, or the bytes read from stdin."""
    return source if isinstance(source, Path) else io.BytesIO(source)


def decode_image(source: Path | bytes, detected_format: str) -> Image.Image:
    """Decode the (primary) image of a HEIC or HEIF, or the image of a format Pillow reads (the first frame of a GIF).

    libheif applies the HEIF transformations (`irot`, `imir`) to the pixels and pillow-heif resets the EXIF
    orientation. Raises CorruptImageError when the decoder fails.
    """
    try:
        if detected_format in HEIF_FORMATS:
            return pillow_heif.open_heif(_open(source)).to_pillow()
        image = Image.open(_open(source))
        try:
            image.load()
        except BaseException:
            image.close()
            raise
        return image
    except DECODING_ERRORS as e:
        raise CorruptImageError(str(e).strip()) from e


def corruption_reason(source: Path | bytes, detected_format: str) -> str | None:
    """The decoder's message when a file of a checked format can't be decoded to the end, else None.

    An image too large for Pillow (DecompressionBombError, above ~179 megapixels) is not decoded, so not judged.
    """
    if detected_format not in PILLOW_FORMATS + HEIF_FORMATS:
        return None
    try:
        decode_image(source, detected_format).close()
    except CorruptImageError as e:
        return str(e)
    except Image.DecompressionBombError:
        return None
    return None
