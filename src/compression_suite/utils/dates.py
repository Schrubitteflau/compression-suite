"""Dates as exiftool reads them, recognised only in a standard format, never guessed from free text.

exiftool reads EXIF and XMP dates as `YYYY:MM:DD HH:MM:SS`, and converts a PNG `Creation Time` in RFC 1123 (the form
the PNG spec recommends) to it; other tools write ISO 8601. Anything else (a `%c` date in the user's locale, as
gnome-screenshot writes) is free text: exiftool would parse its digits greedily into another date.
"""

import re
from datetime import datetime, timedelta, timezone

# `YYYY:MM:DD` or `YYYY-MM-DD`, then `T` or a space, the time with optional fraction, and an optional zone
DATE = re.compile(
    r"(\d{4})([:-])(\d{2})\2(\d{2})[T ](\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,6}))?(Z|[+-]\d{2}:\d{2})?",
    re.ASCII,
)


def parse_date(value: object) -> datetime | None:
    """The moment `value` gives, aware when it has a zone, or None if it is no date in a recognised format."""
    if not isinstance(value, str):
        return None
    match = DATE.fullmatch(value.strip())
    if match is None:
        return None
    year, _, month, day, hour, minute, second, fraction, zone = match.groups()
    tzinfo = None
    if zone == "Z":
        tzinfo = timezone.utc
    elif zone is not None:
        sign = -1 if zone[0] == "-" else 1
        tzinfo = timezone(sign * timedelta(hours=int(zone[1:3]), minutes=int(zone[4:6])))
    try:
        return datetime(
            int(year),
            int(month),
            int(day),
            int(hour),
            int(minute),
            int(second),
            int((fraction or "0").ljust(6, "0")),
            tzinfo=tzinfo,
        )
    except ValueError:  # out of range: a 13th month, a 25th hour...
        return None


def exif_date(moment: datetime) -> str:
    """`moment` as exiftool writes a date: `YYYY:MM:DD HH:MM:SS`, its fraction and zone if any."""
    text = moment.strftime("%Y:%m:%d %H:%M:%S")
    if moment.microsecond:
        text += f".{moment.microsecond:06d}".rstrip("0")
    offset = moment.utcoffset()
    if offset is not None:
        minutes = int(offset.total_seconds()) // 60
        sign = "-" if minutes < 0 else "+"
        text += f"{sign}{abs(minutes) // 60:02d}:{abs(minutes) % 60:02d}"
    return text


def same_moment(before: object, after: object) -> bool:
    """Whether both are dates in a recognised format giving the same moment (a date with a zone is never the same as
    one without)."""
    moment = parse_date(before)
    return moment is not None and moment == parse_date(after)
