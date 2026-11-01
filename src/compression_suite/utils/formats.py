"""Detect a file's real format from its content (magic bytes), never from its extension."""

from pathlib import Path

# Bytes needed to recognise every format below (enough for an `ftyp` box with its compatible brands).
HEADER_SIZE = 64

UNKNOWN_FORMAT = "unknown"

# ISO base media file format brands (bytes 8-12 of the `ftyp` box).
_AVIF_BRANDS = {b"avif", b"avis"}
_HEIC_BRANDS = {b"heic", b"heix", b"heim", b"heis", b"hevc", b"hevx", b"hevm", b"hevs"}
_HEIF_BRANDS = {b"mif1", b"msf1"}


def detect_format(header: bytes) -> str:
    """Return a lowercase format name (`jpeg`, `png`, `webp`, `avif`, `heic`, ...) or `unknown`."""
    if header.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return "webp"
    if header.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if header.startswith(b"%PDF-"):
        return "pdf"
    if header[4:8] == b"ftyp":
        brands = _ftyp_brands(header)
        if brands & _AVIF_BRANDS:
            return "avif"
        if brands & _HEIC_BRANDS:
            return "heic"
        if brands & _HEIF_BRANDS:
            return "heif"
    return UNKNOWN_FORMAT


def _ftyp_brands(header: bytes) -> set[bytes]:
    """Major brand plus compatible brands of the leading `ftyp` box (skipping the minor version)."""
    box_end = min(int.from_bytes(header[:4], "big"), len(header))
    compatible = header[16:box_end]
    return {header[8:12]} | {compatible[i : i + 4] for i in range(0, len(compatible) - 3, 4)}


def detect_file_format(path: Path) -> str:
    """Detect the format of a file from its first bytes."""
    with path.open("rb") as f:
        return detect_format(f.read(HEADER_SIZE))


# Bytes needed to recognise the video containers below (two MPEG-TS packets, 192 bytes each in an M2TS).
VIDEO_HEADER_SIZE = 256

# Top-level boxes a QuickTime file older than the `ftyp` box may start with.
_OLD_QUICKTIME_BOXES = {b"moov", b"mdat", b"wide", b"free", b"skip"}


def detect_video_container(header: bytes) -> str | None:
    """Return the container of a video file (`mp4`, `mov`, `3gp`, `matroska`, `webm`, `avi`, `mpegts`) or None.

    Only the container is recognised, not what it holds: an ISO media file may hold audio only, or be an image (check
    `detect_format` first). Kept apart from `detect_format`, so that what `inspect` reports doesn't change.
    """
    if header[4:8] == b"ftyp":
        brand = header[8:12]
        if brand == b"qt  ":
            return "mov"
        return "3gp" if brand.startswith(b"3g") else "mp4"
    if header[4:8] in _OLD_QUICKTIME_BOXES:
        return "mov"
    if header.startswith(b"\x1a\x45\xdf\xa3"):
        return "webm" if b"webm" in header[:64] else "matroska"
    if header[:4] == b"RIFF" and header[8:12] == b"AVI ":
        return "avi"
    if header[0:1] == b"\x47" and header[188:189] == b"\x47":
        return "mpegts"
    if header[4:5] == b"\x47" and header[196:197] == b"\x47":  # M2TS (AVCHD): a 4-byte timestamp before each packet
        return "mpegts"
    return None


def detect_file_video_container(path: Path) -> str | None:
    """Detect the video container of a file from its first bytes."""
    with path.open("rb") as f:
        return detect_video_container(f.read(VIDEO_HEADER_SIZE))
