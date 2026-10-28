"""Motion photo detection, `{detected, signals}`: used by compress-image, meant to be reused by inspect.

A motion photo carries a video after its image, which a re-encoding drops. Signals, in this order:

- `xmp_google_motion_photo`: XMP `GCamera:MotionPhoto` = 1;
- `xmp_google_micro_video`: XMP `GCamera:MicroVideo` = 1 (the older Google format);
- `xmp_container_video`: a video item in the XMP `Container:Directory`;
- `samsung_motion_photo_data`: the Samsung end-of-file trailer (`SEFT`) has a `MotionPhoto_Data` entry;
- `trailing_data`: bytes after the end of the image that no known structure explains. The end of the image is the
  EOI of the main JPEG image, the end of the PNG `IEND` chunk or the RIFF size of a WebP. The images declared by a
  JPEG's MPF segment and the entries and index of a readable `SEFT` zone are known structures; a `SEFT` zone without
  `MotionPhoto_Data` thus gives no signal, while an unreadable index, or bytes between its entries, are unexplained.
  A file whose end can't be found (truncated, unknown format, AVIF) is not checked for trailing bytes.

The XMP is read in the tags of exiftool (`-j -G1`); the end of the image and the trailer, in the bytes.
"""

import struct
from typing import Any

from compression_suite.utils.formats import detect_format
from compression_suite.utils.seft import SeftError, read_seft

XMP_GOOGLE_MOTION_PHOTO = "xmp_google_motion_photo"
XMP_GOOGLE_MICRO_VIDEO = "xmp_google_micro_video"
XMP_CONTAINER_VIDEO = "xmp_container_video"
SAMSUNG_MOTION_PHOTO_DATA = "samsung_motion_photo_data"
TRAILING_DATA = "trailing_data"

SAMSUNG_MOTION_PHOTO_ENTRY = "MotionPhoto_Data"

# A byte range of the file, end excluded.
Span = tuple[int, int]


def detect_motion_photo(data: bytes, tags: dict[str, Any]) -> dict[str, Any]:
    """Look for every motion photo signal in a file's bytes and its exiftool tags."""
    signals = [*_xmp_signals(tags), *_trailer_signals(data)]
    return {"detected": bool(signals), "signals": signals}


def _xmp_signals(tags: dict[str, Any]) -> list[str]:
    """The Google signals, read in the XMP tags."""
    signals = []
    if str(tags.get("XMP-GCamera:MotionPhoto")) == "1":
        signals.append(XMP_GOOGLE_MOTION_PHOTO)
    if str(tags.get("XMP-GCamera:MicroVideo")) == "1":
        signals.append(XMP_GOOGLE_MICRO_VIDEO)
    mimes = tags.get("XMP-GContainer:DirectoryItemMime", [])
    if any(str(mime).startswith("video/") for mime in (mimes if isinstance(mimes, list) else [mimes])):
        signals.append(XMP_CONTAINER_VIDEO)
    return signals


def _trailer_signals(data: bytes) -> list[str]:
    """The Samsung signal and `trailing_data`, read in the bytes after the image."""
    signals = []
    try:
        zone = read_seft(data)
    except SeftError:
        zone = None  # its bytes stay unexplained
    if zone is not None and zone.entry(SAMSUNG_MOTION_PHOTO_ENTRY) is not None:
        signals.append(SAMSUNG_MOTION_PHOTO_DATA)

    image = _image_end(data)
    if image is not None:
        image_end, following_images = image
        known = following_images + (zone.described_spans() if zone is not None else [])
        if _has_gap(image_end, len(data), known):
            signals.append(TRAILING_DATA)
    return signals


def _image_end(data: bytes) -> tuple[int, list[Span]] | None:
    """Where the image ends and the images declared to follow it, or None if the format or the file can't tell."""
    fmt = detect_format(data[:64])
    try:
        if fmt == "jpeg":
            return _jpeg_end(data)
        if fmt == "png":
            return _png_end(data), []
        if fmt == "webp":
            return _webp_end(data), []
    except (struct.error, ValueError, IndexError):
        return None
    return None


def _has_gap(start: int, end: int, known: list[Span]) -> bool:
    """Whether some byte of [start, end) lies in none of the known spans."""
    cursor = start
    for span_start, span_end in sorted(known):
        if span_start > cursor:
            return cursor < end
        cursor = max(cursor, span_end)
    return cursor < end


# --- JPEG ------------------------------------------------------------------------

_SOS = 0xDA
_EOI = 0xD9
_APP2 = 0xE2
_STANDALONE_MARKERS = {0x01, *range(0xD0, 0xD8)}  # TEM and RST0-7 have no length
_MPF_IDENTIFIER = b"MPF\0"
_MP_ENTRY_TAG = 0xB002


def _jpeg_end(data: bytes) -> tuple[int, list[Span]]:
    """End of the main image's EOI, and the images its MPF segment declares after it."""
    pos = 2  # after SOI
    mpf_images: list[Span] = []
    while True:
        if data[pos] != 0xFF:
            raise ValueError(f"no JPEG marker at {pos}")
        marker = data[pos + 1]
        if marker == 0xFF:  # fill byte
            pos += 1
            continue
        if marker == _EOI:
            return pos + 2, mpf_images
        if marker in _STANDALONE_MARKERS:
            pos += 2
            continue
        (length,) = struct.unpack_from(">H", data, pos + 2)
        segment_start, segment_end = pos + 4, pos + 2 + length
        if marker == _APP2 and data[segment_start : segment_start + 4] == _MPF_IDENTIFIER:
            mpf_images = _mpf_images(data, segment_start + 4)
        pos = segment_end
        if marker == _SOS:
            pos = _entropy_coded_end(data, pos)


def _entropy_coded_end(data: bytes, pos: int) -> int:
    """Position of the first marker after a scan's entropy-coded data (`FF00` is a stuffed byte, `FFD0`-`FFD7` RST)."""
    while True:
        pos = data.find(b"\xff", pos)
        if pos < 0 or pos + 1 >= len(data):
            raise ValueError("JPEG scan without end")
        following = data[pos + 1]
        if following != 0x00 and not 0xD0 <= following <= 0xD7:
            return pos
        pos += 2


def _mpf_images(data: bytes, tiff_start: int) -> list[Span]:
    """Spans of the images an MPF segment declares, but the first (the main image, at offset 0).

    The MP Entry tag lists 16 bytes per image: attributes, size and offset (relative to the MPF TIFF header).
    """
    byte_order = {b"II": "<", b"MM": ">"}.get(data[tiff_start : tiff_start + 2])
    if byte_order is None:
        return []
    (ifd_offset,) = struct.unpack_from(f"{byte_order}I", data, tiff_start + 4)
    ifd = tiff_start + ifd_offset
    (count,) = struct.unpack_from(f"{byte_order}H", data, ifd)
    for i in range(count):
        tag, _, length, value_offset = struct.unpack_from(f"{byte_order}HHII", data, ifd + 2 + 12 * i)
        if tag != _MP_ENTRY_TAG:
            continue
        spans = []
        for entry in range(tiff_start + value_offset, tiff_start + value_offset + length - 15, 16):
            _, size, offset = struct.unpack_from(f"{byte_order}III", data, entry)
            start, end = tiff_start + offset, tiff_start + offset + size
            # Only a JPEG inside the file is an image: a bogus entry must not explain trailing bytes
            if offset != 0 and end <= len(data) and data[start : start + 3] == b"\xff\xd8\xff":
                spans.append((start, end))
        return spans
    return []


# --- PNG and WebP ----------------------------------------------------------------


def _png_end(data: bytes) -> int:
    """End of the `IEND` chunk (length, type, data and CRC)."""
    pos = 8  # after the signature
    while True:
        length, chunk_type = struct.unpack_from(">I4s", data, pos)
        pos += 12 + length
        if chunk_type == b"IEND":
            return pos
        if pos > len(data):
            raise ValueError("PNG without IEND")


def _webp_end(data: bytes) -> int:
    """End of the RIFF chunk, from its size."""
    (size,) = struct.unpack_from("<I", data, 4)
    end = 8 + size
    if end > len(data):
        raise ValueError("truncated WebP")
    return end
