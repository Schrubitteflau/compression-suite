"""The orientation of an image: the rotation or mirror a viewer applies to its stored pixels before displaying them.

Tinify applies the EXIF orientation to the pixels of the image it returns (seen on real calls, for a rotation and for a
mirror), without any EXIF left. Copying the original's orientation back would make a viewer apply it a second time.
Whether an image shows the same picture as another, as displayed, is read on small grayscale copies of their pixels.
"""

import io
import math
from pathlib import Path

from PIL import Image, ImageChops, ImageOps, ImageStat

# The tags giving the orientation, in the order a viewer reads them (Pillow's exif_transpose too): the EXIF wins.
ORIENTATION_TAGS = ("IFD0:Orientation", "XMP-tiff:Orientation")

NORMAL_ORIENTATION = "Horizontal (normal)"

# exiftool's names of the orientations a viewer displays with width and height swapped.
AXES_SWAPPING_ORIENTATIONS = (
    "Mirror horizontal and rotate 270 CW",
    "Rotate 90 CW",
    "Mirror horizontal and rotate 90 CW",
    "Rotate 270 CW",
)

# Size of the grayscale copies compared, and the mean difference (out of 255) below which two images show the same
# picture. Re-encoding by Tinify differs by less than 1, a rotation or a mirror of a real photo by tens.
COMPARED_SIZE = (32, 32)
SAME_PICTURE_MAX_DIFFERENCE = 8

# Width/height ratios further apart than this can't be the same picture.
ASPECT_RATIO_TOLERANCE = 0.02


def orientation(tags: dict) -> str | None:
    """The orientation a viewer applies, from exiftool tags (`Group:Name`), or None if no tag gives it."""
    return next((tags[tag] for tag in ORIENTATION_TAGS if tag in tags), None)


def is_oriented(tags: dict) -> bool:
    """Whether a viewer turns or mirrors the stored pixels before displaying them."""
    return orientation(tags) not in (None, NORMAL_ORIENTATION)


def displayed_size(width: int, height: int, tags: dict) -> tuple[int, int]:
    """The size of the image as displayed: width and height swapped by a quarter turn."""
    return (height, width) if orientation(tags) in AXES_SWAPPING_ORIENTATIONS else (width, height)


def _grayscale_copy(source: Path | bytes, oriented: bool) -> tuple[Image.Image, float] | None:
    """A small grayscale copy of the pixels, as displayed or as stored, and their width/height ratio.

    None if Pillow can't decode the image. A JPEG is decoded at a reduced scale, which is much faster.
    """
    try:
        with Image.open(source if isinstance(source, Path) else io.BytesIO(source)) as image:
            image.draft("L", (COMPARED_SIZE[0] * 8, COMPARED_SIZE[1] * 8))
            pixels = ImageOps.exif_transpose(image) if oriented else image.copy()
            return pixels.convert("L").resize(COMPARED_SIZE), pixels.width / pixels.height
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError):
        return None


def _difference(first: tuple[Image.Image, float], second: tuple[Image.Image, float]) -> float:
    """The mean difference of two grayscale copies, infinite if their ratios tell them apart."""
    (first_pixels, first_ratio), (second_pixels, second_ratio) = first, second
    if abs(first_ratio - second_ratio) > ASPECT_RATIO_TOLERANCE * first_ratio:
        return math.inf
    return ImageStat.Stat(ImageChops.difference(first_pixels, second_pixels)).mean[0]


def stored_size(source: Path | bytes) -> tuple[int, int]:
    """The width and height of the stored pixels, read without decoding them."""
    with Image.open(source if isinstance(source, Path) else io.BytesIO(source)) as image:
        return image.size


def orientation_applied(original: Path, compressed: bytes) -> bool:
    """Whether the stored pixels of the compressed image are those of the original as displayed rather than as stored.

    They must show the same picture as the original displayed, and be closer to it than to the original stored. False
    when they can't be told apart, or either can't be decoded: the original's orientation is then copied back, as for
    any other tag, and an output displayed otherwise is caught by same_displayed_picture.
    """
    displayed, stored = _grayscale_copy(original, oriented=True), _grayscale_copy(original, oriented=False)
    compressed_copy = _grayscale_copy(compressed, oriented=False)
    if displayed is None or stored is None or compressed_copy is None:
        return False
    to_displayed = _difference(compressed_copy, displayed)
    return to_displayed <= SAME_PICTURE_MAX_DIFFERENCE and to_displayed < _difference(compressed_copy, stored)


def same_displayed_picture(original: Path, output: Path) -> bool | None:
    """Whether a viewer displays the output as the original, each with its own orientation; None if not decoded."""
    first, second = _grayscale_copy(original, oriented=True), _grayscale_copy(output, oriented=True)
    if first is None or second is None:
        return None
    return _difference(first, second) <= SAME_PICTURE_MAX_DIFFERENCE
