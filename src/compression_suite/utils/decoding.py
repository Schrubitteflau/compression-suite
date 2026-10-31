"""Decode an image to the end, to tell a damaged file (truncated, cut by an interrupted sync) from a sound one.

A file is damaged when its format is recognised but its decoder can't read it to the end. Only the formats a pinned
decoder reads are checked: Pillow for JPEG, PNG, WebP, GIF and AVIF, pillow-heif for HEIC and HEIF (called directly,
not registered as a Pillow plugin). Any other format, PDF included, is never said to be damaged.

libheif decodes a grid image (an iPhone photo is made of 512 px tiles) whose tiles are missing without an error, and
fills them in. A HEIC or HEIF is therefore also damaged when its item locations (`iloc` box) point past the end of
the file: the file was cut short.
"""

import io
import struct
from pathlib import Path

import pillow_heif
from PIL import Image

from compression_suite.utils.contract import CommandError, ErrorCode

PILLOW_FORMATS = ("jpeg", "png", "webp", "gif", "avif")
HEIF_FORMATS = ("heic", "heif")

# libheif's default security limits: a layout declaring more is not read here, libheif refuses it (a damaged file)
MAX_ITEMS = 1000
MAX_EXTENTS_PER_ITEM = 32
# What libheif's message holds when it refuses a file beyond those limits
LIBHEIF_SECURITY_LIMIT = "Security limit exceeded"

# What the decoders raise on a file they can't read (Pillow raises SyntaxError for a truncated AVIF, for instance)
DECODING_ERRORS = (OSError, SyntaxError, ValueError, EOFError)


class CorruptImageError(Exception):
    """A file whose format is recognised but that its decoder can't read to the end; the message is the decoder's."""

    def command_message(self, detected_format: str) -> str:
        """The message of the `input_corrupt` error, shown as it is by WelcomeApp."""
        return f"Damaged {detected_format} file, it can't be decoded: {self}"


class IncompleteImageError(CorruptImageError):
    """A HEIC or HEIF that libheif decodes, but whose data runs past the end of the file: it was cut short."""

    def command_message(self, detected_format: str) -> str:
        return f"Damaged {detected_format} file: {self}"


def _decoder_input(source: Path | bytes) -> Path | io.BytesIO:
    """What the decoders open: the path of a file, or the bytes read from stdin."""
    return source if isinstance(source, Path) else io.BytesIO(source)


def _boxes(data: bytes, start: int, end: int):
    """The (type, payload start, payload end) of the ISO BMFF boxes between start and end."""
    while start + 8 <= end:
        size, box_type = struct.unpack_from(">I4s", data, start)
        header = 8
        if size == 1:
            (size,) = struct.unpack_from(">Q", data, start + 8)
            header = 16
        elif size == 0:
            size = end - start
        if size < header:
            return
        yield box_type, start + header, min(start + size, end)
        start += size


def _item_data_end(data: bytes) -> int | None:
    """Where the data of the HEIF items stored in the file (`iloc` box, in `meta`) ends; None if it can't be told."""
    meta = next(((s, e) for t, s, e in _boxes(data, 0, len(data)) if t == b"meta"), None)
    if meta is None:
        return None
    iloc = next(((s, e) for t, s, e in _boxes(data, meta[0] + 4, meta[1]) if t == b"iloc"), None)  # meta: FullBox
    if iloc is None:
        return None
    pos = iloc[0]
    version = data[pos]
    offset_size, length_size = data[pos + 4] >> 4, data[pos + 4] & 0xF
    base_offset_size, index_size = data[pos + 5] >> 4, data[pos + 5] & 0xF
    if version not in (1, 2):
        index_size = 0
    pos += 6

    def read(size: int) -> int:
        nonlocal pos
        if size not in (0, 2, 4, 8):
            raise ValueError(f"field size {size}")
        value = int.from_bytes(data[pos : pos + size], "big") if size else 0
        if pos + size > iloc[1]:
            raise ValueError("iloc box cut short")
        pos += size
        return value

    end = 0
    id_size = 4 if version == 2 else 2
    item_count = read(id_size)
    if item_count > MAX_ITEMS:
        raise ValueError(f"{item_count} items")
    for _ in range(item_count):
        read(id_size)  # item ID
        construction_method = read(2) & 0xF if version in (1, 2) else 0
        data_reference_index = read(2)
        base_offset = read(base_offset_size)
        extent_count = read(2)
        if extent_count > MAX_EXTENTS_PER_ITEM:
            raise ValueError(f"{extent_count} extents")
        for _ in range(extent_count):
            read(index_size)
            extent_offset, extent_length = read(offset_size), read(length_size)
            # Only data in this file, at a file offset: not in the `idat` box (1), another item (2) or another file.
            # An extent without a length runs to the end of the file: only its start can be checked
            if construction_method == 0 and data_reference_index == 0:
                end = max(end, base_offset + extent_offset + extent_length)
    return end


def missing_item_data(data: bytes) -> str | None:
    """Why a HEIC or HEIF is cut short (its items' data runs past the end of the file), else None.

    A file whose layout can't be read is left to libheif to judge.
    """
    try:
        end = _item_data_end(data)
    except (struct.error, IndexError, ValueError):
        return None
    if end is None or end <= len(data):
        return None
    return f"file cut short, its image data runs to byte {end} but the file has {len(data)}"


def decode_image(source: Path | bytes, detected_format: str) -> Image.Image:
    """Decode the (primary) image of a HEIC or HEIF, or the image of a format Pillow reads (the first frame of a GIF).

    libheif applies the HEIF transformations (`irot`, `imir`) to the pixels and pillow-heif resets the EXIF
    orientation. Raises CorruptImageError when the decoder fails, or when a HEIC or HEIF is cut short: libheif
    errors only on some missing data (a single image's), not on missing tiles of a grid.
    """
    try:
        if detected_format in HEIF_FORMATS:
            try:
                image = pillow_heif.open_heif(_decoder_input(source)).to_pillow()
            except RuntimeError as e:
                # libheif refusing a file beyond its security limits: damaged. Any other error of libheif (lacking
                # memory, for instance) says nothing about the file and is left to the generic handling.
                if LIBHEIF_SECURITY_LIMIT not in str(e):
                    raise
                raise CorruptImageError(str(e).strip()) from e
            reason = missing_item_data(source if isinstance(source, bytes) else source.read_bytes())
            if reason is not None:
                image.close()
                raise IncompleteImageError(reason)
            return image
        image = Image.open(_decoder_input(source))
        try:
            image.load()
        except BaseException:
            image.close()
            raise
        return image
    except DECODING_ERRORS as e:
        raise CorruptImageError(str(e).strip()) from e


def _corruption(source: Path | bytes, detected_format: str) -> CorruptImageError | None:
    """Why a file of a checked format is damaged, else None.

    An image too large for Pillow (DecompressionBombError, above ~179 megapixels) is not decoded, so not judged.
    """
    if detected_format not in PILLOW_FORMATS + HEIF_FORMATS:
        return None
    try:
        decode_image(source, detected_format).close()
    except CorruptImageError as e:
        return e
    except Image.DecompressionBombError:
        pass
    return None


def corruption_reason(source: Path | bytes, detected_format: str) -> str | None:
    """The decoder's message (or why the file is cut short) when a file of a checked format is damaged, else None."""
    error = _corruption(source, detected_format)
    return None if error is None else str(error)


def corrupt_input_error(detected_format: str, error: CorruptImageError) -> CommandError:
    """The `input_corrupt` error of a file whose format is recognised but that is damaged."""
    return CommandError(
        ErrorCode.INPUT_CORRUPT,
        error.command_message(detected_format),
        {"detected_format": detected_format},
    )


def check_decodable(source: Path | bytes, detected_format: str) -> None:
    """Reject a damaged input (`input_corrupt`) before any work on it."""
    error = _corruption(source, detected_format)
    if error is not None:
        raise corrupt_input_error(detected_format, error)
