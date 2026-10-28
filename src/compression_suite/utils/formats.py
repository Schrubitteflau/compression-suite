"""Detect a file's real format from its content (magic bytes), never from its extension."""

from pathlib import Path

# Bytes needed to recognise every format below (enough for an `ftyp` box with its compatible brands).
HEADER_SIZE = 64

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
    return "unknown"


def _ftyp_brands(header: bytes) -> set[bytes]:
    """Major brand plus compatible brands of the leading `ftyp` box (skipping the minor version)."""
    box_end = min(int.from_bytes(header[:4], "big"), len(header))
    compatible = header[16:box_end]
    return {header[8:12]} | {compatible[i : i + 4] for i in range(0, len(compatible) - 3, 4)}


def detect_file_format(path: Path) -> str:
    """Detect the format of a file from its first bytes."""
    with path.open("rb") as f:
        return detect_format(f.read(HEADER_SIZE))
