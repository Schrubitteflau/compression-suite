"""Metadata diff between an original file and its processed copy, read by the pinned exiftool on every group.

Tags are keyed `Group:Name` (exiftool `-j -G1 -a`). Each tag present before is either kept, an expected loss
(declared by the command, with its reason) or an unexpected loss (`removed` or `changed`). Tags that appear
after are listed apart. The important fields (capture date, GPS, camera...) always come first, even when intact.

The module is generic: a command passes its own expected losses. The video commands will add their important
fields (rotation, creation date) to IMPORTANT_FIELDS.
"""

import json
import subprocess
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

from PIL import Image

from compression_suite.utils.contract import CommandError, ErrorCode
from compression_suite.utils.orientation import ORIENTATION_TAGS, displayed_size, orientation

# Not metadata of the image: file system attributes, values computed by exiftool, exiftool's own version and warnings.
IGNORED_GROUPS = ("System", "Composite", "ExifTool")

REENCODING_REASON = "réencodage"

# The `File` group describes the encoding, which re-encoding changes on purpose. Its dimensions must not change,
# and its JPEG comment is real metadata.
FILE_GROUP_COMPARED_TAGS = ("File:ImageWidth", "File:ImageHeight", "File:Comment")

# Groups carrying the image dimensions, by format. They are compared as displayed (orientation applied): a change of
# the displayed dimensions is never expected, whatever the command declares (even `--metadata strip`), the image must
# keep its size. Stored dimensions swapped with the pixels by a quarter turn, the displayed ones kept, are expected.
DIMENSION_GROUPS = ("File", "PNG", "RIFF")
DIMENSION_TAGS = tuple(f"{group}:{name}" for group in DIMENSION_GROUPS for name in ("ImageWidth", "ImageHeight"))
ROTATED_DIMENSIONS_REASON = "pixels pivotés selon l'orientation : l'image affichée garde ses dimensions"

# Tags that describe an alpha channel, by format. Re-encoding may change them, or drop an alpha channel without any
# transparent pixel, but losing a real transparency, or not being able to check it, is never expected.
ALPHA_TAGS = (
    "PNG:ColorType",
    "PNG:Transparency",
    "RIFF:WebP_Flags",
    "RIFF:AlphaPreprocessing",
    "RIFF:AlphaFiltering",
    "RIFF:AlphaCompression",
    "QuickTime:AuxiliaryImageType",
)

# The transparency and the picture displayed are read on the decoded pixels, not in the tags.
PIXELS_SOURCE = "pixels"
TRANSPARENCY_SOURCE = PIXELS_SOURCE

# Synthetic tag of a transparency loss read on the pixels, which no alpha tag may show (an RGBA output made opaque).
TRANSPARENCY_TAG = f"{PIXELS_SOURCE}:transparency"

# Synthetic tag of an output a viewer displays otherwise than the original (turned or mirrored), read on the pixels.
ORIENTATION_TAG = f"{PIXELS_SOURCE}:orientation"

MTIME_SOURCE = "file_mtime"


@dataclass(frozen=True)
class ExpectedLoss:
    """A tag, or an fnmatch pattern of `Group:Name` tags, lost on purpose, with the reason shown to the user.

    `excluded_tag` is the exiftool tag name a metadata copy excludes on purpose to cause this loss, if any.
    """

    pattern: str
    reason: str
    excluded_tag: str | None = None


# A candidate source of an important field: one tag (scalar value), or named parts (dict value) whose first part
# must be present. The first tag of a candidate is the field's `source`.
Candidate = str | dict[str, str]


@dataclass(frozen=True)
class ImportantField:
    """A field always shown first, read from the first candidate present.

    A `displayed` field is a `{width, height}` given as displayed, swapped when the orientation turns the image.
    """

    key: str
    candidates: tuple[Candidate, ...]
    mtime_fallback: bool = False
    displayed: bool = False


DIMENSIONS_FIELD = ImportantField(
    "dimensions",
    tuple({"width": f"{group}:ImageWidth", "height": f"{group}:ImageHeight"} for group in DIMENSION_GROUPS),
    displayed=True,
)

IMPORTANT_FIELDS = (
    ImportantField(
        "capture_date",
        (
            "ExifIFD:DateTimeOriginal",
            "XMP-exif:DateTimeOriginal",
            "XMP-photoshop:DateCreated",
            "XMP-xmp:CreateDate",
            "PNG:CreationTime",
        ),
        mtime_fallback=True,
    ),
    ImportantField(
        "gps",
        (
            {
                "latitude": "GPS:GPSLatitude",
                "latitude_ref": "GPS:GPSLatitudeRef",
                "longitude": "GPS:GPSLongitude",
                "longitude_ref": "GPS:GPSLongitudeRef",
                "altitude": "GPS:GPSAltitude",
                "altitude_ref": "GPS:GPSAltitudeRef",
            },
            {
                "latitude": "XMP-exif:GPSLatitude",
                "longitude": "XMP-exif:GPSLongitude",
                "altitude": "XMP-exif:GPSAltitude",
                "altitude_ref": "XMP-exif:GPSAltitudeRef",
            },
        ),
    ),
    ImportantField(
        "camera",
        ({"make": "IFD0:Make", "model": "IFD0:Model"}, {"make": "XMP-tiff:Make", "model": "XMP-tiff:Model"}),
    ),
    DIMENSIONS_FIELD,
    ImportantField("orientation", ORIENTATION_TAGS),
    ImportantField("color_profile", ("ICC_Profile:ProfileDescription",)),
)


def read_metadata(exiftool: Path, path: Path) -> dict[str, Any]:
    """Read every tag of a file (`Group:Name` -> value), without the ignored groups.

    exiftool still describes a file whose format it doesn't know or that it refuses (with an `ExifTool:Error` or
    `ExifTool:Warning`, in an ignored group): no output at all means exiftool itself failed (`internal_error`),
    never the file. A damaged file is told by its decoder (utils.decoding), not by exiftool.
    """
    result = subprocess.run(
        [str(exiftool), "-j", "-G1", "-a", str(path)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    try:
        [tags] = json.loads(result.stdout)
    except ValueError as e:
        raise CommandError(
            ErrorCode.INTERNAL_ERROR, f"exiftool failed on {path} (exit {result.returncode}): {result.stderr.strip()}"
        ) from e
    return {
        tag: value for tag, value in tags.items() if tag != "SourceFile" and tag.split(":", 1)[0] not in IGNORED_GROUPS
    }


def pixel_transparency(path: Path) -> bool | None:
    """Whether at least one pixel of the image is not fully opaque, or None if Pillow can't decode it."""
    try:
        with Image.open(path) as image:
            if image.mode not in ("RGBA", "LA", "PA", "La", "RGBa") and "transparency" not in image.info:
                return False
            return image.convert("RGBA").getchannel("A").getextrema()[0] < 255
    except (OSError, ValueError, Image.DecompressionBombError):
        return None


def _transparency_field(transparency: tuple[bool | None, bool | None]) -> dict[str, Any]:
    """The important field telling whether the image keeps its transparent pixels."""
    before, after = transparency
    if before is None or after is None:
        status = "unverified"
    elif before == after:
        status = "same"
    else:
        status = "lost" if before else "changed"
    return {"key": "transparency", "before": before, "after": after, "source": TRANSPARENCY_SOURCE, "status": status}


def _transparency_loss(transparency: tuple[bool | None, bool | None]) -> dict[str, Any] | None:
    """The unexpected loss of a real transparency, `removed` or `unverified`, or None if none could be lost.

    An original known to be opaque has no transparency to lose, even when the output can't be decoded.
    """
    before, after = transparency
    if before is False:
        return None
    if before is True and after is False:
        kind = "removed"
    elif before is None or after is None:
        kind = "unverified"
    else:
        return None
    return {"tag": TRANSPARENCY_TAG, "kind": kind, "before": before, "after": after}


def _candidate_value(candidate: Candidate, tags: dict[str, Any]) -> tuple[Any, str] | None:
    """The candidate's value and source in `tags`, or None if its first tag is missing."""
    if isinstance(candidate, str):
        return (tags[candidate], candidate) if candidate in tags else None
    source = next(iter(candidate.values()))
    if source not in tags:
        return None
    return {part: tags[tag] for part, tag in candidate.items() if tag in tags}, source


def _field_value(field: ImportantField, tags: dict[str, Any]) -> tuple[Any, str | None]:
    """The value of an important field and the tag it comes from, or (None, None)."""
    for candidate in field.candidates:
        found = _candidate_value(candidate, tags)
        if found is None:
            continue
        value, source = found
        if field.displayed and {"width", "height"} <= value.keys():
            width, height = displayed_size(value["width"], value["height"], tags)
            value = {"width": width, "height": height}
        return value, source
    return None, None


def important_values(tags: dict[str, Any], mtime: str | None, transparency: bool | None) -> list[dict[str, Any]]:
    """Every important field of a single file, in the diff's order, with its value and source (inspect).

    `mtime` is the file's ISO 8601 mtime, the capture date of last resort. `transparency` is None, and so is its
    source, when Pillow can't decode the file.
    """
    fields = []
    for field in IMPORTANT_FIELDS:
        value, source = _field_value(field, tags)
        if source is None and field.mtime_fallback and mtime is not None:
            value, source = mtime, MTIME_SOURCE
        fields.append({"key": field.key, "value": value, "source": source})
    transparency_source = TRANSPARENCY_SOURCE if transparency is not None else None
    fields.append({"key": "transparency", "value": transparency, "source": transparency_source})
    return fields


def _status(before: Any, after: Any) -> str:
    """`absent` on both sides, `same`, `lost` after, or `changed` (including a value that appears)."""
    if before is None and after is None:
        return "absent"
    if before == after:
        return "same"
    if after is None:
        return "lost"
    return "changed"


def _important_fields(
    before: dict[str, Any],
    after: dict[str, Any],
    mtimes: tuple[str, str] | None,
    transparency: tuple[bool | None, bool | None],
) -> list[dict[str, Any]]:
    """Every important field, in a fixed order, with its value before and after (the transparency last)."""
    fields = []
    for field in IMPORTANT_FIELDS:
        before_value, before_source = _field_value(field, before)
        after_value, after_source = _field_value(field, after)
        source = before_source or after_source
        if source is None and field.mtime_fallback and mtimes is not None:
            (before_value, after_value), source = mtimes, MTIME_SOURCE
        fields.append(
            {
                "key": field.key,
                "before": before_value,
                "after": after_value,
                "source": source,
                "status": _status(before_value, after_value),
            }
        )
    fields.append(_transparency_field(transparency))
    return fields


def _swapped_dimension_tags(before: dict[str, Any], after: dict[str, Any]) -> set[str]:
    """The dimension tags of the groups whose width and height were swapped with the pixels, the displayed dimensions
    kept: the stored image was turned by a quarter turn, without changing its size as displayed."""
    if _field_value(DIMENSIONS_FIELD, before)[0] != _field_value(DIMENSIONS_FIELD, after)[0]:
        return set()
    swapped = set()
    for group in DIMENSION_GROUPS:
        width, height = f"{group}:ImageWidth", f"{group}:ImageHeight"
        if {width, height} <= before.keys() & after.keys() and (before[width], before[height]) == (
            after[height],
            after[width],
        ):
            swapped.update((width, height))
    return swapped


def _never_expected_tags(transparency: tuple[bool | None, bool | None], swapped_dimensions: set[str]) -> set[str]:
    """Tags whose loss is unexpected whatever the command declares: the dimensions unless swapped with the pixels, and
    the alpha tags unless the original is known to be opaque or the output known to be transparent."""
    never_expected = set(DIMENSION_TAGS) - swapped_dimensions
    before, after = transparency
    if before is not False and after is not True:
        never_expected.update(ALPHA_TAGS)
    return never_expected


def _expected_reason(tag: str, expected_losses: list[ExpectedLoss], never_expected: set[str]) -> str | None:
    """The reason of a loss: that of dimensions swapped with the pixels (the only dimension tags not never expected),
    of the first expected loss matching the tag, the `File` group's, or None if the loss is unexpected."""
    if tag in never_expected:
        return None
    if tag in DIMENSION_TAGS:
        return ROTATED_DIMENSIONS_REASON
    for loss in expected_losses:
        if fnmatchcase(tag, loss.pattern):
            return loss.reason
    if tag.startswith("File:") and tag not in FILE_GROUP_COMPARED_TAGS:
        return REENCODING_REASON
    return None


def diff_metadata(
    before: dict[str, Any],
    after: dict[str, Any],
    expected_losses: list[ExpectedLoss],
    transparency: tuple[bool | None, bool | None],
    mtimes: tuple[str, str] | None = None,
    same_picture: bool | None = None,
) -> dict[str, Any]:
    """Classify the differences between the tags of the original (`before`) and of its processed copy (`after`).

    `transparency` tells, for both files, whether a pixel is not fully opaque (`pixel_transparency`).
    `mtimes` are the ISO 8601 mtimes of both files, the capture date of last resort. Leave it out when the
    original's mtime is unknown (stdin). `same_picture` tells whether a viewer displays the copy as the original
    (`same_displayed_picture`), None when it wasn't checked.
    """
    expected, unexpected = [], []
    never_expected = _never_expected_tags(transparency, _swapped_dimension_tags(before, after))
    for tag, value in before.items():
        if tag in after and after[tag] == value:
            continue
        reason = _expected_reason(tag, expected_losses, never_expected)
        if reason is not None:
            expected.append({"tag": tag, "before": value, "reason": reason})
        elif tag in after:
            unexpected.append({"tag": tag, "kind": "changed", "before": value, "after": after[tag]})
        else:
            unexpected.append({"tag": tag, "kind": "removed", "before": value, "after": None})
    transparency_loss = _transparency_loss(transparency)
    if transparency_loss is not None:
        unexpected.append(transparency_loss)
    if same_picture is False:
        unexpected.append(
            {"tag": ORIENTATION_TAG, "kind": "changed", "before": orientation(before), "after": orientation(after)}
        )
    return {
        "important": _important_fields(before, after, mtimes, transparency),
        "unexpected_losses": unexpected,
        "expected_losses": expected,
        "added": [{"tag": tag, "after": value} for tag, value in after.items() if tag not in before],
        "before": before,
        "after": after,
    }
