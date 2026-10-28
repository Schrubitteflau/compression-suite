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

# Not metadata of the image: file system attributes, values computed by exiftool, exiftool's own version and warnings.
IGNORED_GROUPS = ("System", "Composite", "ExifTool")

REENCODING_REASON = "réencodage"

# The `File` group describes the encoding, which re-encoding changes on purpose. Its dimensions must not change,
# and its JPEG comment is real metadata.
FILE_GROUP_COMPARED_TAGS = ("File:ImageWidth", "File:ImageHeight", "File:Comment")

# Groups carrying the image dimensions, by format. A change of dimensions is never expected, whatever the command
# declares: the image must keep its size.
DIMENSION_GROUPS = ("File", "PNG", "RIFF")
DIMENSION_TAGS = tuple(f"{group}:{name}" for group in DIMENSION_GROUPS for name in ("ImageWidth", "ImageHeight"))

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
    """A field always shown first, read from the first candidate present."""

    key: str
    candidates: tuple[Candidate, ...]
    mtime_fallback: bool = False


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
    ImportantField(
        "dimensions",
        tuple(
            {"width": f"{group}:ImageWidth", "height": f"{group}:ImageHeight"}
            for group in DIMENSION_GROUPS
        ),
    ),
    ImportantField("orientation", ("IFD0:Orientation", "XMP-tiff:Orientation")),
    ImportantField("color_profile", ("ICC_Profile:ProfileDescription",)),
)


def read_metadata(exiftool: Path, path: Path) -> dict[str, Any]:
    """Read every tag of a file (`Group:Name` -> value), without the ignored groups.

    exiftool still describes a file whose format it doesn't know (with an `ExifTool:Error`, in an ignored group):
    no output at all means exiftool itself failed, which is an error rather than a file without tags.
    """
    result = subprocess.run(
        [str(exiftool), "-j", "-G1", "-a", str(path)], capture_output=True, text=True, timeout=30,
    )
    try:
        [tags] = json.loads(result.stdout)
    except ValueError as e:
        raise RuntimeError(f"exiftool could not read {path}: {result.stderr.strip()}") from e
    return {
        tag: value
        for tag, value in tags.items()
        if tag != "SourceFile" and tag.split(":", 1)[0] not in IGNORED_GROUPS
    }


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
        if found is not None:
            return found
    return None, None


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
    before: dict[str, Any], after: dict[str, Any], mtimes: tuple[str, str] | None,
) -> list[dict[str, Any]]:
    """Every important field, in a fixed order, with its value before and after."""
    fields = []
    for field in IMPORTANT_FIELDS:
        before_value, before_source = _field_value(field, before)
        after_value, after_source = _field_value(field, after)
        source = before_source or after_source
        if source is None and field.mtime_fallback and mtimes is not None:
            (before_value, after_value), source = mtimes, MTIME_SOURCE
        fields.append({
            "key": field.key,
            "before": before_value,
            "after": after_value,
            "source": source,
            "status": _status(before_value, after_value),
        })
    return fields


def _expected_reason(tag: str, expected_losses: list[ExpectedLoss]) -> str | None:
    """The reason of the first expected loss matching the tag, the `File` group's, or None if the loss is unexpected."""
    if tag in DIMENSION_TAGS:
        return None
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
    mtimes: tuple[str, str] | None = None,
) -> dict[str, Any]:
    """Classify the differences between the tags of the original (`before`) and of its processed copy (`after`).

    `mtimes` are the ISO 8601 mtimes of both files, the capture date of last resort. Leave it out when the
    original's mtime is unknown (stdin).
    """
    expected, unexpected = [], []
    for tag, value in before.items():
        if tag in after and after[tag] == value:
            continue
        reason = _expected_reason(tag, expected_losses)
        if reason is not None:
            expected.append({"tag": tag, "before": value, "reason": reason})
        elif tag in after:
            unexpected.append({"tag": tag, "kind": "changed", "before": value, "after": after[tag]})
        else:
            unexpected.append({"tag": tag, "kind": "removed", "before": value, "after": None})
    return {
        "important": _important_fields(before, after, mtimes),
        "unexpected_losses": unexpected,
        "expected_losses": expected,
        "added": [{"tag": tag, "after": value} for tag, value in after.items() if tag not in before],
        "before": before,
        "after": after,
    }
