"""Important fields and metadata diff of a video, in the shape of utils.metadata_diff's (same tags, same lists).

A video's important fields are its creation date, GPS, camera, dimensions (as displayed) and rotation. The rotation
and the dimensions are read on the video track (`TrackN` group whose handler is a video one), whichever its number.

The tracks of an export are numbered anew (a data track dropped before the audio, a cover art first): `match_tracks`
names each track of the export after the original track it comes from, so that the diff compares a track with its
own original, and a track the export doesn't keep shows as lost as a whole.
"""

import math
import re
from collections.abc import Callable
from datetime import datetime, timedelta
from fnmatch import fnmatchcase
from typing import Any

from compression_suite.utils.metadata_diff import (
    FILE_GROUP_COMPARED_TAGS,
    MTIME_SOURCE,
    REENCODING_REASON,
    Candidate,
    ExpectedLoss,
    added_tags,
    candidate_value,
    classify_losses,
    field_status,
)

TRACK_GROUP = re.compile(r"Track(\d+)")

# How exiftool names the video track's handler (QuickTime) or type (Matroska). A MOV gives each track a second
# handler (`URL`, its data reference), which may be the one kept: a visual sample entry (`SourceImageWidth`) tells too.
VIDEO_TRACK_TYPES = {"HandlerType": "Video Track", "TrackType": "Video"}
VISUAL_SAMPLE_TAG = "SourceImageWidth"

# A QuickTime date never set
ZERO_DATE = "0000:00:00 00:00:00"

# Stored sizes x265 crops to an even number in 4:2:0 (its last column or row)
SIZE_TAGS = ("ImageWidth", "ImageHeight", "SourceImageWidth", "SourceImageHeight")
EVEN_CROP_REASON = "largeur ou hauteur impaire : dernière colonne ou ligne retirée (x265 en 4:2:0)"

# The movie's creation date, stored in UTC, and the offset of the local time a Galaxy records (`+0200`)
MOVIE_DATE = "QuickTime:CreateDate"
ANDROID_TIME_ZONE = "Keys:AndroidTimeZone"
UTC_OFFSET = re.compile(r"([+-])(\d\d):?(\d\d)")
EXIFTOOL_DATE = "%Y:%m:%d %H:%M:%S"

CAPTURE_DATE_CANDIDATES: tuple[Candidate, ...] = (
    "Keys:CreationDate",  # Apple, with its time zone
    MOVIE_DATE,  # the movie's, in UTC: given in local time by `_local_movie_date`
    "Info:DateTimeOriginal",  # Matroska
    "RIFF:DateTimeOriginal",
    "XMP-xmp:CreateDate",
)
GPS_CANDIDATES: tuple[Candidate, ...] = (
    {"coordinates": "Keys:GPSCoordinates"},  # Apple
    {"coordinates": "UserData:GPSCoordinates"},  # Android (`©xyz`)
)
CAMERA_CANDIDATES: tuple[Candidate, ...] = (
    {"make": "Keys:Make", "model": "Keys:Model"},  # Apple
    {"make": "Keys:AndroidMake", "model": "Keys:AndroidModel"},
    {"make": "UserData:Make", "model": "UserData:Model"},
    {"model": "Samsung:SamsungModel"},  # Samsung's `smta` box
)

# Every important field: its value and the tag it comes from, or (None, None)
FieldReader = Callable[[dict[str, Any]], tuple[Any, str | None]]


def _first(candidates: tuple[Candidate, ...]) -> FieldReader:
    """A reader of the first candidate present, a date never set counting as absent."""

    def read(tags: dict[str, Any]) -> tuple[Any, str | None]:
        for candidate in candidates:
            found = candidate_value(candidate, tags)
            if found is not None and found[0] != ZERO_DATE:
                return found
        return None, None

    return read


def _local_movie_date(value: Any, tags: dict[str, Any]) -> Any:
    """The movie's creation date (UTC) in the form of Apple's `Keys:CreationDate`: in the local time of the offset a
    Galaxy records (`Keys:AndroidTimeZone`, `+0200` -> `2024:06:01 10:00:00+02:00`), else in UTC with `+00:00`. A
    value exiftool doesn't give as a plain date is left as is."""
    try:
        date = datetime.strptime(str(value), EXIFTOOL_DATE)
    except ValueError:
        return value
    zone = UTC_OFFSET.fullmatch(str(tags.get(ANDROID_TIME_ZONE, "")).strip())
    sign, hours, minutes = zone.groups() if zone else ("+", "00", "00")
    offset = timedelta(hours=int(hours), minutes=int(minutes)) * (-1 if sign == "-" else 1)
    return f"{(date + offset).strftime(EXIFTOOL_DATE)}{sign}{hours}:{minutes}"


def capture_date(tags: dict[str, Any]) -> tuple[Any, str | None]:
    """The creation date (see CAPTURE_DATE_CANDIDATES), the movie's in local time, and the tag it comes from."""
    value, source = _first(CAPTURE_DATE_CANDIDATES)(tags)
    return (_local_movie_date(value, tags), source) if source == MOVIE_DATE else (value, source)


def track_groups(tags: dict[str, Any]) -> list[str]:
    """The track groups of the tags (`Track1`, `Track2`...), by number."""
    groups = {tag.split(":")[0] for tag in tags if TRACK_GROUP.fullmatch(tag.split(":")[0])}
    return sorted(groups, key=lambda group: int(group[5:]))


def video_track(tags: dict[str, Any]) -> str | None:
    """The group of the first video track (`Track1`...), or None."""
    for group in track_groups(tags):
        if f"{group}:{VISUAL_SAMPLE_TAG}" in tags or any(
            tags.get(f"{group}:{name}") == value for name, value in VIDEO_TRACK_TYPES.items()
        ):
            return group
    return None


def rotation(tags: dict[str, Any]) -> tuple[int | None, str | None]:
    """The clockwise rotation a player applies to the video track, in degrees, read on its display matrix."""
    group = video_track(tags)
    source = f"{group}:MatrixStructure"
    try:
        a, b = (float(value) for value in str(tags[source]).split()[:2])
    except (KeyError, ValueError):
        return None, None
    return round(math.degrees(math.atan2(b, a))) % 360, source


def dimensions(tags: dict[str, Any]) -> tuple[dict[str, int] | None, str | None]:
    """The video track's `{width, height}` as displayed, its rotation applied."""
    group = video_track(tags)
    width, height = tags.get(f"{group}:ImageWidth"), tags.get(f"{group}:ImageHeight")
    if width is None or height is None:
        return None, None
    if rotation(tags)[0] in (90, 270):
        width, height = height, width
    return {"width": width, "height": height}, f"{group}:ImageWidth"


# In the order of the image fields they share; the rotation stands where an image's orientation does
VIDEO_IMPORTANT_FIELDS: tuple[tuple[str, FieldReader], ...] = (
    ("capture_date", capture_date),
    ("gps", _first(GPS_CANDIDATES)),
    ("camera", _first(CAMERA_CANDIDATES)),
    ("dimensions", dimensions),
    ("rotation", rotation),
)
MTIME_FALLBACK_FIELD = "capture_date"


def video_important_values(tags: dict[str, Any], mtime: str | None) -> list[dict[str, Any]]:
    """Every important field of a single video, with its value and source (inspect). `mtime` is the file's ISO 8601
    mtime, the capture date of last resort."""
    fields = []
    for key, read in VIDEO_IMPORTANT_FIELDS:
        value, source = read(tags)
        if source is None and key == MTIME_FALLBACK_FIELD and mtime is not None:
            value, source = mtime, MTIME_SOURCE
        fields.append({"key": key, "value": value, "source": source})
    return fields


def match_tracks(before: dict[str, Any], after: dict[str, Any], origins: list[int | None]) -> dict[str, Any]:
    """The tags of an export, each track's group named after the original track it comes from.

    `origins` gives, for each track of the export in order (`Track1`, `Track2`...), the track ID (`TrackID`) of the
    original track it comes from, None for a track of its own (ffmpeg's timecode track). A track of its own keeps its
    name unless an original track has it: it then takes the next number free.
    """
    by_id = {before[f"{group}:TrackID"]: group for group in track_groups(before) if f"{group}:TrackID" in before}
    names = {f"Track{number}": by_id[track_id] for number, track_id in enumerate(origins, start=1) if track_id in by_id}
    taken = {tag.split(":")[0] for tag in before} | set(names.values())
    last = max((int(group[5:]) for group in [*track_groups(before), *track_groups(after)]), default=0)
    for group in track_groups(after):
        if group not in names and group in taken:
            last += 1
            names[group] = f"Track{last}"
    renamed = {}
    for tag, value in after.items():
        group, name = tag.split(":", 1)
        renamed[f"{names.get(group, group)}:{name}"] = value
    return renamed


def _protected(tag: str, kept_groups: set[str]) -> bool:
    """A date, GPS or rotation tag of the movie or of a track the export keeps: its loss is never expected."""
    group, name = tag.split(":", 1)
    if TRACK_GROUP.fullmatch(group) and group not in kept_groups:
        return False
    return "Date" in name or "GPS" in name or name == "MatrixStructure"


def _odd_size_cropped(tag: str, before: dict[str, Any], after: dict[str, Any]) -> bool:
    """A stored width or height, odd, that x265 cropped by one."""
    value, cropped = before.get(tag), after.get(tag)
    return tag.split(":", 1)[1] in SIZE_TAGS and isinstance(value, int) and value % 2 == 1 and cropped == value - 1


def diff_video_metadata(
    before: dict[str, Any],
    after: dict[str, Any],
    expected_losses: list[ExpectedLoss],
    mtimes: tuple[str, str] | None = None,
) -> dict[str, Any]:
    """Classify the differences between the tags of an original video (`before`) and of its export (`after`, its
    tracks named after the original's by `match_tracks`).

    A loss is expected when it matches `expected_losses` (in order), is a `File` field other than the dimensions, or
    an odd stored size x265 cropped by one, never when it is a date, GPS or rotation tag of the movie or of a track
    the export keeps. `mtimes` are the ISO 8601 mtimes of both files, the capture date of last resort.
    """
    kept_groups = {tag.split(":")[0] for tag in after}

    def reason(tag: str) -> str | None:
        if _protected(tag, kept_groups):
            return None
        if _odd_size_cropped(tag, before, after):
            return EVEN_CROP_REASON
        for loss in expected_losses:
            if fnmatchcase(tag, loss.pattern):
                return loss.reason
        if tag.startswith("File:") and tag not in FILE_GROUP_COMPARED_TAGS:
            return REENCODING_REASON
        return None

    important = []
    for key, read in VIDEO_IMPORTANT_FIELDS:
        (before_value, before_source), (after_value, after_source) = read(before), read(after)
        source = before_source or after_source
        if source is None and key == MTIME_FALLBACK_FIELD and mtimes is not None:
            (before_value, after_value), source = mtimes, MTIME_SOURCE
        status = field_status(before_value, after_value)
        important.append({"key": key, "before": before_value, "after": after_value, "source": source, "status": status})
    unexpected, expected = classify_losses(before, after, reason)
    return {
        "important": important,
        "unexpected_losses": unexpected,
        "expected_losses": expected,
        "added": added_tags(before, after),
        "before": before,
        "after": after,
    }
