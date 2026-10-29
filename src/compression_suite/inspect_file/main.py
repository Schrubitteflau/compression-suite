"""Core logic for inspect: describe any file (format, size, dates, important fields, metadata, motion photo).

It never contacts Tinify and needs no key. A format nobody knows is not an error, nor is a damaged file, which it
still describes and flags as `corrupt`: only a missing or unreadable input, or a missing or failing pinned tool, make
it fail.
"""

import mmap
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from compression_suite.compress_image.main import SUPPORTED_FORMATS, iso_mtime, validate_input_file
from compression_suite.utils.decoding import corruption_reason
from compression_suite.utils.formats import UNKNOWN_FORMAT, detect_file_format
from compression_suite.utils.metadata_diff import important_values, pixel_transparency, read_metadata
from compression_suite.utils.motion_photo import detect_motion_photo
from compression_suite.utils.pinned_tools import resolve_tool
from compression_suite.utils.seft import FileBytes

DEFAULT_MIME_TYPE = "application/octet-stream"

# MIME types of the formats detect_format recognises; exiftool names the others.
MIME_TYPES = {
    "jpeg": "image/jpeg",
    "png": "image/png",
    "webp": "image/webp",
    "gif": "image/gif",
    "pdf": "application/pdf",
    "avif": "image/avif",
    "heic": "image/heic",
    "heif": "image/heif",
}


@contextmanager
def mapped_file(path: Path) -> Iterator[FileBytes]:
    """The file's bytes, mapped rather than read: a video may weigh gigabytes. An empty file can't be mapped."""
    with path.open("rb") as f:
        if path.stat().st_size == 0:
            yield b""
            return
        with mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as data:
            yield data


def file_format(detected_format: str, tags: dict[str, Any]) -> tuple[str, str]:
    """The format detected on the content, and its MIME type.

    detect_format comes first, as for compress-image; exiftool, which also reads the content, names the rest.
    """
    exiftool_mime_type = tags.get("File:MIMEType", DEFAULT_MIME_TYPE)
    if detected_format != UNKNOWN_FORMAT:
        return detected_format, MIME_TYPES.get(detected_format, exiftool_mime_type)
    exiftool_type = tags.get("File:FileType")
    return (str(exiftool_type).lower() if exiftool_type is not None else UNKNOWN_FORMAT), exiftool_mime_type


def inspect_file(input_file: str) -> dict[str, Any]:
    """Describe a file and return the `result` object of the JSON contract."""
    exiftool = resolve_tool("exiftool")
    path = validate_input_file(input_file)
    stat = path.stat()
    mtime = iso_mtime(stat)
    tags = read_metadata(exiftool, path)
    detected_format = detect_file_format(path)
    fmt, mime_type = file_format(detected_format, tags)
    with mapped_file(path) as data:
        motion_photo = detect_motion_photo(data, tags)
    corrupt_reason = corruption_reason(path, detected_format)
    return {
        "path": input_file,
        "format": fmt,
        "mime_type": mime_type,
        "size_bytes": stat.st_size,
        "mtime": mtime,
        "compressible": detected_format in SUPPORTED_FORMATS,  # what compress-image itself checks
        "motion_photo": motion_photo,
        "corrupt": {"detected": corrupt_reason is not None, "reason": corrupt_reason},
        "important": important_values(tags, mtime, pixel_transparency(path)),
        "metadata": tags,
    }
