"""The colours of an image as a viewer that manages colours displays them: its pixels converted to sRGB with its ICC
profile, or read as sRGB without one.

Tinify converts the pixels to sRGB with the ICC profile and keeps no profile (seen on real calls, for a P3 JPEG, PNG,
WebP and AVIF). Copying the original's P3 profile back would make a viewer read these sRGB pixels as P3: oversaturated
colours. Colours are compared on small RGB copies of the pixels as displayed (orientation applied), in sRGB.
"""

import io
from pathlib import Path

from PIL import Image, ImageChops, ImageCms, ImageOps, ImageStat

from compression_suite.utils.orientation import ASPECT_RATIO_TOLERANCE

SRGB = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB"))

# Size of the RGB copies compared, and the largest mean difference of a channel (out of 255) below which two images
# show the same colours. On the S23 P3 sample, compressed by Tinify (JPEG, PNG, WebP, AVIF), the conversion to sRGB
# leaves at most 1.05; its P3 pixels read as sRGB, or its sRGB pixels read as P3, differ by 2.7 or more.
COMPARED_SIZE = (32, 32)
SAME_COLORS_MAX_DIFFERENCE = 1.5

# Modes an ICC profile can describe as they are; any other (palette, alpha) is flattened on white to RGB first.
PROFILED_MODES = ("RGB", "L", "CMYK")


def _open(source: Path | bytes) -> Image.Image:
    return Image.open(source if isinstance(source, Path) else io.BytesIO(source))


def has_icc_profile(source: Path | bytes) -> bool:
    """Whether the image carries an ICC profile, read without decoding the pixels; False if Pillow can't open it."""
    try:
        with _open(source) as image:
            return bool(image.info.get("icc_profile"))
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError):
        return False


def _srgb_copy(source: Path | bytes, use_profile: bool) -> tuple[Image.Image, float] | None:
    """A small RGB copy of the pixels as displayed, in sRGB, and their width/height ratio.

    With `use_profile`, the pixels are converted from the image's ICC profile; without, or without a profile, they
    are read as sRGB. None if Pillow can't decode the image or apply its profile.
    """
    try:
        with _open(source) as image:
            icc_profile = image.info.get("icc_profile") if use_profile else None
            image.draft("RGB", (COMPARED_SIZE[0] * 8, COMPARED_SIZE[1] * 8))
            pixels = ImageOps.exif_transpose(image)
            if pixels.mode not in PROFILED_MODES:
                pixels = Image.alpha_composite(Image.new("RGBA", pixels.size, "white"), pixels.convert("RGBA"))
                pixels = pixels.convert("RGB")
            if icc_profile:
                profile = ImageCms.ImageCmsProfile(io.BytesIO(icc_profile))
                converted = ImageCms.profileToProfile(pixels, profile, SRGB, outputMode="RGB")
                assert converted is not None  # only None with inPlace=True
                pixels = converted
            return pixels.convert("RGB").resize(COMPARED_SIZE), pixels.width / pixels.height
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError, ImageCms.PyCMSError):
        return None


def _difference(first: tuple[Image.Image, float], second: tuple[Image.Image, float]) -> float | None:
    """The largest mean difference of a channel between two RGB copies, None if their ratios tell them apart."""
    (first_pixels, first_ratio), (second_pixels, second_ratio) = first, second
    if abs(first_ratio - second_ratio) > ASPECT_RATIO_TOLERANCE * first_ratio:
        return None
    return max(ImageStat.Stat(ImageChops.difference(first_pixels, second_pixels)).mean)


def colors_converted(original: Path, compressed: bytes) -> bool:
    """Whether the compressed pixels, read with their own profile if any, are the original's converted to sRGB.

    They must show the original's colours converted, and the original's pixels read as sRGB must not: a profile close
    to sRGB, which changes nothing, can't be told apart. False then, or when either can't be decoded: the original's
    profile is copied back, as any other tag, and colours displayed otherwise are caught by same_colors.
    """
    converted, raw = _srgb_copy(original, use_profile=True), _srgb_copy(original, use_profile=False)
    compressed_copy = _srgb_copy(compressed, use_profile=True)
    if converted is None or raw is None or compressed_copy is None:
        return False
    to_converted, to_raw = _difference(compressed_copy, converted), _difference(compressed_copy, raw)
    if to_converted is None or to_raw is None:
        return False
    return to_converted <= SAME_COLORS_MAX_DIFFERENCE < to_raw


def same_colors(original: Path, output: Path) -> bool | None:
    """Whether a viewer that manages colours displays the output with the original's colours, each converted to sRGB
    with its own profile, or read as sRGB without one.

    None when neither has a profile (nothing but re-encoding touched the colours), when either can't be decoded, or
    when they don't show the same picture (their ratios differ).
    """
    if not has_icc_profile(original) and not has_icc_profile(output):
        return None
    first, second = _srgb_copy(original, use_profile=True), _srgb_copy(output, use_profile=True)
    if first is None or second is None:
        return None
    difference = _difference(first, second)
    return None if difference is None else difference <= SAME_COLORS_MAX_DIFFERENCE
