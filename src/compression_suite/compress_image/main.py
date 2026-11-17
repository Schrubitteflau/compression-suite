"""Core logic for compress-image: Tinify API compression with metadata preservation."""

import io
import os
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, TypeVar

import tinify
from PIL import Image
from rich.console import Console
from tinify import AccountError, ClientError, ServerError
from tinify import ConnectionError as TinifyConnectionError

from compression_suite.utils.colors import (
    SIXTEEN_BIT_GRAY_MODES,
    colors_converted,
    eight_bit_gray,
    has_icc_profile,
    same_colors,
)
from compression_suite.utils.config import resolve_api_key
from compression_suite.utils.contract import CommandError, ErrorCode
from compression_suite.utils.dates import exif_date, parse_date, same_moment
from compression_suite.utils.decoding import check_decodable
from compression_suite.utils.formats import HEADER_SIZE, detect_file_format, detect_format
from compression_suite.utils.metadata_diff import (
    REENCODING_REASON,
    ExpectedLoss,
    diff_metadata,
    pixel_transparency,
    read_metadata,
)
from compression_suite.utils.motion_photo import (
    SAMSUNG_MOTION_PHOTO_DATA,
    TRAILING_DATA,
    detect_motion_photo,
    image_end,
)
from compression_suite.utils.orientation import (
    ORIENTATION_TAGS,
    is_oriented,
    orientation_applied,
    same_displayed_picture,
    stored_size,
)
from compression_suite.utils.pinned_tools import resolve_tool
from compression_suite.utils.seft import SeftError, SeftZone, read_seft

T = TypeVar("T")

HARD_LIMIT_BYTES = 15 * 1024 * 1024  # 15 MB

SUPPORTED_FORMATS = ("jpeg", "png", "webp", "avif")

# Inputs `--format jpeg` accepts: a PNG is converted, a JPEG compressed as is.
JPEG_OUTPUT_INPUT_FORMATS = ("jpeg", "png")

# Encoding of the JPEG a PNG is converted to before Tinify, not an option: why, and the measurements behind it, are in
# the command's docstring (cli.py).
JPEG_QUALITY = 92
JPEG_SUBSAMPLING = "4:2:0"

THUMBNAIL_REASON = "miniature EXIF retirée exprès : elle montrerait l'image d'origine"
STRIP_REASON = "métadonnées retirées à la demande"

# Tags of exiftool's Samsung::Trailer table, read in the SEFT trailer, which is appended back after the compressed
# image (see zone_to_append). Their group, `Samsung`, is also the one of Samsung's EXIF maker notes, which are copied
# back: `Samsung:*` would hide their loss.
SAMSUNG_TRAILER_TAGS = (
    "DepthMapData",
    "DepthMapData2",
    "DepthMapName",
    "DualCameraImage",
    "DualCameraImageName",
    "EmbeddedAudioFile",
    "EmbeddedAudioFileName",
    "EmbeddedImage",
    "EmbeddedImage2",
    "EmbeddedImageName",
    "EmbeddedVideoFile",
    "EmbeddedVideoOffsetSize",
    "EmbeddedVideoType",
    "MCCData",
    "MotionPhotoAutoPlayVideo",
    "OriginalPathHashKey",
    "RemasterInfo",
    "SamsungCaptureInfo",
    "SamsungMotionPhotoVersion",
    "SingleShotDepthMap",
    "SurroundShotVideo",
    "SurroundShotVideoName",
    "TimeStamp",
    "VideoEditedTimeZone",
)

# Losses decided by compression-suite, shown with their reason in the metadata diff. Those with an
# `excluded_tag` are what EXIFTOOL_EXCLUDE_TAGS keeps from being copied back, so both can't diverge.
# A loss found on a real sample must first be fixed by a copy (EXIFTOOL_EXTRA_TAGS): only a loss
# that can't be avoided belongs here.
EXPECTED_LOSSES = [
    ExpectedLoss("IFD1:ThumbnailImage", THUMBNAIL_REASON, excluded_tag="ThumbnailImage"),
    ExpectedLoss("IFD1:Compression", "propre à la miniature EXIF, retirée avec elle", excluded_tag="Compression"),
    ExpectedLoss("IFD1:*", THUMBNAIL_REASON),  # size, offset and length of the removed thumbnail
    # Encoding parameters outside the File group, which Tinify chooses anew
    *(
        ExpectedLoss(tag, REENCODING_REASON)
        for tag in (
            "PNG:BitDepth",
            "PNG:ColorType",
            "PNG:Compression",
            "PNG:Filter",
            "PNG:Interlace",
            "PNG:Palette",
            "PNG:SignificantBits",
            "PNG:Transparency",
            "QuickTime:ImagePixelDepth",
            "QuickTime:MediaDataSize",
            "QuickTime:MediaDataOffset",
        )
    ),
]

EXIFTOOL_EXCLUDE_TAGS = [f"-{loss.excluded_tag}=" for loss in EXPECTED_LOSSES if loss.excluded_tag]

CONVERTED_REASON = "format converti en JPEG"

# Losses of a PNG converted to JPEG: the fields of the PNG encoding, which a JPEG has no place for. Its dimensions are
# compared as displayed, and its ICC profile is attached to the JPEG without the PNG's name for it.
CONVERTED_LOSSES = [
    ExpectedLoss(f"PNG:{name}", CONVERTED_REASON)
    for name in (
        "ImageWidth",
        "ImageHeight",
        "BitDepth",
        "ColorType",
        "Compression",
        "Filter",
        "Interlace",
        "Palette",
        "ProfileName",
        "SignificantBits",
    )
]

# PNG text blocks with a standard equivalent in a JPEG, rewritten to it when a PNG is converted, so that photo managers
# read them: `Group:Name` of the block as exiftool reads it -> `Group:Name` of its JPEG tag. Any other text block has
# no place in a JPEG and is lost, unexpectedly. A block is never written over a tag the PNG holds in its EXIF or XMP,
# which the photo manager that wrote it reads first. No date is ever derived from the file's mtime, nor guessed from a
# `Creation Time` in free text.
TEXT_BLOCK_TARGETS = {
    "PNG:Title": "XMP-dc:Title",
    "PNG:Description": "XMP-dc:Description",
    "PNG:Author": "XMP-dc:Creator",
    "PNG:Copyright": "XMP-dc:Rights",
    "PNG:Comment": "File:Comment",
    "PNG:Software": "IFD0:Software",
    "PNG:CreationTime": "XMP-xmp:CreateDate",
}

CREATION_TIME = "PNG:CreationTime"


MOTION_PHOTO_REASON = "photo animée : perte autorisée"

# Losses of a motion photo compressed with --allow-motion-photo-loss, losing its video. The XMP declaring the video is
# not copied back: the compressed file would still claim a video it no longer has. Its toolkit, copied back with the
# rest, is dropped if nothing else remains. No SEFT trailer is appended back, so its fields are lost too.
MOTION_PHOTO_LOSSES = [
    ExpectedLoss("XMP-GCamera:MotionPhoto*", MOTION_PHOTO_REASON, excluded_tag="XMP-GCamera:MotionPhoto*"),
    ExpectedLoss("XMP-GCamera:MicroVideo*", MOTION_PHOTO_REASON, excluded_tag="XMP-GCamera:MicroVideo*"),
    ExpectedLoss("XMP-GContainer:*", MOTION_PHOTO_REASON, excluded_tag="XMP-GContainer:all"),
    ExpectedLoss("XMP-x:XMPToolkit", MOTION_PHOTO_REASON),
    ExpectedLoss("Google:MotionPhotoVideo", MOTION_PHOTO_REASON),
    *(ExpectedLoss(f"Samsung:{tag}", MOTION_PHOTO_REASON) for tag in SAMSUNG_TRAILER_TAGS),
]

EXIFTOOL_MOTION_PHOTO_EXCLUDE_TAGS = [f"-{loss.excluded_tag}=" for loss in MOTION_PHOTO_LOSSES if loss.excluded_tag]

ORIENTATION_REASON = "orientation appliquée aux pixels"

# Tags giving the size of the stored pixels, which a quarter turn applied by Tinify swaps.
STORED_WIDTH_TAGS = ("IFD0:ImageWidth", "ExifIFD:ExifImageWidth")
STORED_HEIGHT_TAGS = ("IFD0:ImageHeight", "ExifIFD:ExifImageHeight")

# Losses of an image whose orientation Tinify applied to the pixels: copied back, the orientation would be applied a
# second time. It is written as normal instead, and the stored size tags take the size of the compressed pixels.
ORIENTATION_APPLIED_LOSSES = [
    ExpectedLoss(tag, ORIENTATION_REASON) for tag in (*ORIENTATION_TAGS, *STORED_WIDTH_TAGS, *STORED_HEIGHT_TAGS)
]

COLORS_CONVERTED_REASON = "couleurs converties en sRGB par Tinify (gamut réduit)"

# Losses of an image whose colours Tinify converted to sRGB with its ICC profile (a P3 photo): copied back, the profile
# would make a viewer read the sRGB pixels as P3, oversaturated. The output is left in sRGB, without profile. exiftool
# names the groups of the profile's parts ICC_Profile, ICC-header, ICC-meas... In an AVIF, Tinify declares the sRGB
# primaries and transfer instead of unspecified ones (seen on a real call).
COLORS_CONVERTED_LOSSES = [
    ExpectedLoss("ICC*:*", COLORS_CONVERTED_REASON, excluded_tag="ICC_Profile"),
    ExpectedLoss("QuickTime:ColorPrimaries", COLORS_CONVERTED_REASON),
    ExpectedLoss("QuickTime:TransferCharacteristics", COLORS_CONVERTED_REASON),
]

EXIFTOOL_COLORS_CONVERTED_EXCLUDE_TAGS = [
    f"-{loss.excluded_tag}=" for loss in COLORS_CONVERTED_LOSSES if loss.excluded_tag
]

# Tags that -all:all doesn't copy and must be listed explicitly (ICC_Profile and the IFD0 dimensions are
# "unsafe" for exiftool: without them, the colour profile of a P3 photo whose colours Tinify left as they were would
# be lost). Without XMPToolkit, exiftool would name itself as the toolkit of the XMP it rewrites.
EXIFTOOL_EXTRA_TAGS = [
    "-InteropIndex",
    "-InteropVersion",
    "-ICC_Profile",
    "-IFD0:ImageWidth",
    "-IFD0:ImageHeight",
    "-XMP-x:XMPToolkit",
]


def validate_input_file(path: str) -> Path:
    """Check that the input exists and is a regular file (`input_not_found`), which can be read (`input_unreadable`)."""
    p = Path(path)
    if not p.is_file():
        reason = "is not a file" if p.exists() else "does not exist"
        raise CommandError(ErrorCode.INPUT_NOT_FOUND, f"Input file {reason}: {path}")
    if not os.access(p, os.R_OK):
        raise CommandError(ErrorCode.INPUT_UNREADABLE, f"Input file is not readable: {path}")
    return p


def check_format(detected_format: str, supported_formats: tuple[str, ...] = SUPPORTED_FORMATS) -> None:
    """Reject any format the command can't handle: by default, those Tinify can't compress."""
    if detected_format not in supported_formats:
        raise CommandError(
            ErrorCode.UNSUPPORTED_FORMAT,
            f"Unsupported format: {detected_format} (supported: {', '.join(supported_formats)})",
            {"detected_format": detected_format},
        )


def check_output(output_path: Path, overwrite: bool) -> None:
    """Refuse to write over a folder, or over an existing file without `--overwrite`."""
    if output_path.is_dir():
        raise CommandError(ErrorCode.OUTPUT_EXISTS, f"Output path is a directory: {output_path}.")
    if output_path.exists() and not overwrite:
        raise CommandError(
            ErrorCode.OUTPUT_EXISTS,
            f"Output file already exists: {output_path}. Use --overwrite to replace.",
        )


def check_size(size: int, disable_hard_limit: bool) -> None:
    """Reject inputs above the hard limit, unless it is disabled."""
    if not disable_hard_limit and size > HARD_LIMIT_BYTES:
        raise CommandError(
            ErrorCode.FILE_TOO_LARGE,
            f"Input exceeds {HARD_LIMIT_BYTES // (1024 * 1024)}MB hard limit ({size:,} bytes). "
            f"Use --disable-hard-limit to override.",
            {"size_bytes": size, "limit_bytes": HARD_LIMIT_BYTES},
        )


def zone_to_append(original: bytes, motion_photo: dict[str, Any], metadata: str) -> SeftZone | None:
    """The original's Samsung trailer (`SEFT`), which Tinify drops, to append back after the compressed image, or None.

    Only with `--metadata keep`: `strip` is for privacy, and the zone carries the operator's country, a UTC timestamp
    and the video. A photo without video gets its zone back. A motion photo only when its zone holds the whole video:
    a Samsung motion photo with nothing unexplained, whose zone starts right at the end of its image. Every byte after
    the image is then appended back as is, so the video stays at the same distance from the end of the image and from
    the end of the file: neither the zone's index nor the Google `Container:Directory` has an offset to change.
    """
    if metadata != "keep":
        return None
    try:
        zone = read_seft(original)
    except SeftError:
        return None
    if zone is None or not motion_photo["detected"]:
        return zone
    signals = motion_photo["signals"]
    whole_video = SAMSUNG_MOTION_PHOTO_DATA in signals and TRAILING_DATA not in signals
    return zone if whole_video and zone.start == image_end(original) else None


def append_zone(compressed: bytes, original: bytes, zone: SeftZone) -> bytes | None:
    """The compressed image followed by the original's zone, in one block, or None if the compressed file doesn't end
    with its image (a zone of its own, other bytes): appended after them, the zone would not follow the image."""
    if image_end(compressed) != len(compressed):
        return None
    return compressed + original[zone.start :]


def check_motion_photo(motion_photo: dict[str, Any], allow_motion_photo_loss: bool) -> None:
    """Reject a motion photo, whose video Tinify would drop, unless its loss is allowed."""
    if motion_photo["detected"] and not allow_motion_photo_loss:
        raise CommandError(
            ErrorCode.MOTION_PHOTO,
            f"Motion photo detected ({', '.join(motion_photo['signals'])}): compressing it would drop its video. "
            f"Use --allow-motion-photo-loss to compress it anyway.",
            {"signals": motion_photo["signals"]},
        )


def call_tinify(tinify_input: bytes) -> bytes:
    """Compress `tinify_input` with Tinify, translating its exceptions into stable error codes."""
    try:
        return tinify.from_buffer(tinify_input).to_buffer()
    except AccountError as e:
        if e.status == 429 or "limit" in str(e).lower():
            raise CommandError(ErrorCode.QUOTA_EXCEEDED, f"API quota exceeded: {e}") from e
        raise CommandError(ErrorCode.API_KEY_INVALID, f"API account error: {e}") from e
    except ClientError as e:
        raise CommandError(ErrorCode.API_KEY_INVALID, f"API client error: {e}") from e
    except ServerError as e:
        raise CommandError(ErrorCode.TINIFY_UNAVAILABLE, f"API server error: {e}") from e
    except TinifyConnectionError as e:
        raise CommandError(ErrorCode.TINIFY_UNAVAILABLE, f"Connection error: {e}") from e


def applied_orientation_tags(original_tags: dict[str, Any], compressed: bytes) -> list[str]:
    """exiftool assignments for pixels Tinify already turned: a normal orientation, the compressed pixels' size.

    Only the tags of the original are written. Applied after the copy in the same command, they win over it.
    """
    width, height = stored_size(compressed)
    assignments = [(tag, "1") for tag in ORIENTATION_TAGS]
    assignments += [(tag, str(width)) for tag in STORED_WIDTH_TAGS] + [(tag, str(height)) for tag in STORED_HEIGHT_TAGS]
    return [f"-{tag}#={value}" for tag, value in assignments if tag in original_tags]


def text_block_tags(original_tags: dict[str, Any]) -> list[str]:
    """exiftool assignments writing the PNG text blocks to their JPEG tags, applied after the copy.

    Only into tags the PNG doesn't hold. A `Creation Time` is written only from a date in a recognised format, in
    exiftool's form: given as is, exiftool would read the digits of any text as a date.
    """
    args = []
    for block, target in TEXT_BLOCK_TARGETS.items():
        if block not in original_tags or target in original_tags:
            continue
        if block == CREATION_TIME:
            moment = parse_date(original_tags[block])
            if moment is not None:
                args.append(f"-{target}={exif_date(moment)}")
        else:
            args.append(f"-{target}<{block}")
    return args


def rewritten_text_blocks(original_tags: dict[str, Any], final_tags: dict[str, Any]) -> list[ExpectedLoss]:
    """The losses of the PNG text blocks rewritten to their JPEG tag, each with a reason naming it.

    A block is rewritten when its tag reads back its value (the same moment, for a date). A value exiftool couldn't
    write whole (a comment too long for a JPEG), or a tag of the PNG holding another value, leaves the block an
    unexpected loss.
    """
    return [
        ExpectedLoss(block, f"bloc texte PNG réécrit en {target}")
        for block, target in TEXT_BLOCK_TARGETS.items()
        if block in original_tags
        and target in final_tags
        and (
            same_moment(original_tags[block], final_tags[target])
            if block == CREATION_TIME
            else str(final_tags[target]) == str(original_tags[block])
        )
    ]


def preserve_metadata(exiftool: Path, original_path: Path, compressed_path: Path, extra_args: list[str]):
    """Copy metadata from original to compressed file using exiftool, excluding problematic tags.

    `extra_args` are further exclusions or assignments, applied after the copy.
    """
    cmd = [
        str(exiftool),
        "-TagsFromFile",
        str(original_path),
        "-all:all",
        *EXIFTOOL_EXTRA_TAGS,
        *EXIFTOOL_EXCLUDE_TAGS,
        *extra_args,
        "-overwrite_original",
        str(compressed_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise CommandError(ErrorCode.METADATA_COPY_FAILED, f"exiftool failed: {result.stderr.strip()}")


@dataclass(frozen=True)
class Original:
    """The input image, read once: the source of the metadata copy, the metadata diff and every comparison.

    `path` and `stat` are None when it came from stdin.
    """

    path: Path | None
    stat: os.stat_result | None
    data: bytes
    format: str

    @property
    def size(self) -> int:
        return len(self.data)


def read_original(input_file: str | None, disable_hard_limit: bool, supported_formats: tuple[str, ...]) -> Original:
    """Read the input from its path, or from stdin when None, rejecting what Tinify can't or shouldn't compress, and
    any format but `supported_formats`.

    A file is checked for its format and size before it is read, so that a huge file is never loaded.
    """
    if input_file is None:
        data = sys.stdin.buffer.read()
        input_format = detect_format(data[:HEADER_SIZE])
        check_format(input_format, supported_formats)
        check_size(len(data), disable_hard_limit)
        check_decodable(data, input_format)
        return Original(path=None, stat=None, data=data, format=input_format)

    path = validate_input_file(input_file)
    stat = path.stat()  # before any read, which may refresh the atime
    input_format = detect_file_format(path)
    check_format(input_format, supported_formats)
    check_size(stat.st_size, disable_hard_limit)
    data = path.read_bytes()
    check_decodable(path, input_format)  # Tinify would fail on it, or compress a broken image
    return Original(path=path, stat=stat, data=data, format=input_format)


def check_opaque(png: bytes) -> None:
    """Refuse a PNG with at least one pixel not fully opaque: JPEG has no transparency, and flattening it onto a
    background would change the picture. The caller compresses it without --format instead."""
    if pixel_transparency(png):
        raise CommandError(
            ErrorCode.TRANSPARENT_INPUT,
            "Transparent PNG: at least one pixel is not fully opaque, and JPEG has no transparency. "
            "Compress it without --format to keep it a PNG.",
        )


# The PNG colour types of grey pixels, without and with alpha (opaque, else refused), read in the IHDR chunk, after the
# signature (8 bytes), the chunk's length and type (8), the width and height (8) and the bit depth (1). Pillow's mode
# doesn't tell them: it opens a 16-bit grey and alpha PNG as RGBA.
GRAY_COLOUR_TYPES = (0, 4)
COLOUR_TYPE_OFFSET = 25


def jpeg_pixels(image: Image.Image, gray: bool) -> Image.Image:
    """The PNG's pixels on 8 bits, in the mode of the JPEG: one component for a grey PNG (8 or 16 bits, alpha or not),
    RGB for any other (16-bit RGB, palette, opaque alpha). Pillow already reads a 16-bit RGB PNG on 8 bits."""
    if not gray:
        return image.convert("RGB")
    return eight_bit_gray(image) if image.mode in SIXTEEN_BIT_GRAY_MODES else image.convert("L")


def convert_to_jpeg(png: bytes) -> bytes:
    """Encode the PNG's pixels as a JPEG, with its ICC profile and no other metadata (copied from the PNG later on)."""
    with Image.open(io.BytesIO(png)) as image:
        icc_profile = image.info.get("icc_profile")
        buf = io.BytesIO()
        jpeg_pixels(image, png[COLOUR_TYPE_OFFSET] in GRAY_COLOUR_TYPES).save(
            buf, format="JPEG", quality=JPEG_QUALITY, subsampling=JPEG_SUBSAMPLING, icc_profile=icc_profile
        )
    return buf.getvalue()


@contextmanager
def file_on_disk(path: Path | None, data: bytes, suffix: str) -> Iterator[Path]:
    """Yield `path`, or a temporary copy of `data` when it came from stdin: exiftool needs a file."""
    if path is not None:
        yield path
        return
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(data)
    try:
        yield Path(tmp.name)
    finally:
        Path(tmp.name).unlink(missing_ok=True)


def current_umask() -> int:
    """Read the process umask (os.umask can only read it by setting it, so it is restored at once)."""
    umask = os.umask(0)
    os.umask(umask)
    return umask


def write_atomically(output_path: Path, data: bytes, finalize: Callable[[Path], T]) -> T:
    """Write to a temporary file in the destination folder, post-process it, then rename it.

    Returns what `finalize` returns.

    The file gets the permissions a plain write would give (0666 minus the umask), not mkstemp's 0600.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=output_path.parent, prefix=f".{output_path.name}.", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        tmp_path.chmod(0o666 & ~current_umask())
        finalized = finalize(tmp_path)
        os.replace(tmp_path, output_path)
        return finalized
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def iso_mtime(stat: os.stat_result) -> str:
    """Format a file's mtime as ISO 8601 in UTC, to the microsecond."""
    return (datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=stat.st_mtime_ns // 1000)).isoformat()


def compress_image(
    input_file: str | None,
    output: str | None,
    api_key: str | None,
    metadata: str,
    output_format: str | None,
    overwrite: bool,
    disable_hard_limit: bool,
    allow_motion_photo_loss: bool,
    verbose: bool,
) -> dict[str, Any]:
    """Compress an image via Tinify API with optional metadata preservation.

    Every check that can fail without Tinify runs before the API call, so that no credit is wasted.

    Args:
        input_file: Path to input file, or None for stdin.
        output: Path to output file, or None for stdout.
        api_key: Tinify key, or None to read it from the configuration file.
        metadata: "keep" to preserve metadata via exiftool, "strip" to discard.
        output_format: "jpeg" to convert a PNG to JPEG before Tinify, or None to keep the input's format.
        overwrite: Allow overwriting existing output file.
        disable_hard_limit: Bypass the 15MB hard limit.
        allow_motion_photo_loss: Compress a motion photo whose video can't be kept, losing its video.
        verbose: Enable verbose reporting.

    Returns:
        The `result` object of the JSON contract.
    """
    console = Console(stderr=True)

    exiftool = resolve_tool("exiftool")
    if verbose:
        console.print(f"[dim]exiftool: {exiftool}[/dim]")

    supported_formats = JPEG_OUTPUT_INPUT_FORMATS if output_format == "jpeg" else SUPPORTED_FORMATS
    original = read_original(input_file, disable_hard_limit, supported_formats)
    # The bytes Tinify compresses, kept apart from the original, which stays the source of the metadata copy, the diff
    # and the comparisons: the original's own bytes, or a PNG converted to JPEG.
    converted = output_format == "jpeg" and original.format == "png"
    if converted:
        check_opaque(original.data)
    tinify_input = convert_to_jpeg(original.data) if converted else original.data

    with file_on_disk(original.path, original.data, f".{original.format}") as exiftool_source:
        original_tags = read_metadata(exiftool, exiftool_source)
        motion_photo = detect_motion_photo(original.data, original_tags)
        zone = zone_to_append(original.data, motion_photo, metadata)
        # The video of a motion photo whose zone is appended back is kept: there is no loss to allow
        check_motion_photo(motion_photo, allow_motion_photo_loss or zone is not None)

        output_path = Path(output) if output is not None else None
        if output_path is not None:
            check_output(output_path, overwrite)

        # tinify declares `key = None  # type: Optional[str]`, a type comment ty does not read
        tinify.key = resolve_api_key(api_key)  # ty: ignore[invalid-assignment]

        compressed_buffer = call_tinify(tinify_input)

        # Appended before the exiftool copy, which keeps the zone byte for byte
        appended_buffer = append_zone(compressed_buffer, original.data, zone) if zone is not None else None
        # None without a motion photo: there is no video to keep
        video_kept = (appended_buffer is not None) if motion_photo["detected"] else None
        motion_photo_exclusions = []
        conversion_losses = CONVERTED_LOSSES if converted else []
        expected_losses = conversion_losses + EXPECTED_LOSSES
        if video_kept is False:
            motion_photo_exclusions = EXIFTOOL_MOTION_PHOTO_EXCLUDE_TAGS
            # Past the check, without a zone, the loss was allowed. A zone that couldn't be appended is unexpected.
            if zone is None:
                expected_losses = MOTION_PHOTO_LOSSES + expected_losses

        # Only an oriented original can be turned twice; with --metadata strip, no orientation is copied back
        oriented = is_oriented(original_tags)
        pixels_turned = metadata == "keep" and oriented and orientation_applied(exiftool_source, compressed_buffer)
        orientation_args = applied_orientation_tags(original_tags, compressed_buffer) if pixels_turned else []
        if pixels_turned:
            expected_losses = ORIENTATION_APPLIED_LOSSES + expected_losses

        text_block_args = text_block_tags(original_tags) if converted else []

        # Only a profile can have converted the colours; with --metadata strip, no profile is copied back
        colors_args = []
        if (
            metadata == "keep"
            and has_icc_profile(exiftool_source)
            and colors_converted(exiftool_source, compressed_buffer)
        ):
            colors_args = EXIFTOOL_COLORS_CONVERTED_EXCLUDE_TAGS
            expected_losses = COLORS_CONVERTED_LOSSES + expected_losses

        def apply_metadata_and_dates(
            compressed_path: Path,
        ) -> tuple[bytes, dict[str, Any], bool | None, bool | None, bool | None]:
            """Copy the metadata, read the final bytes, tags, transparency and whether it is displayed as the original
            (picture and colours), then give the file the original's dates.

            The dates come last: exiftool rewrites the file, and reading it refreshes its atime. Only an oriented
            original is checked for how it is displayed: without orientation, nothing can turn it. The colours of
            another picture than the original's (turned, mirrored) are not compared.
            """
            if metadata == "keep":
                preserve_metadata(
                    exiftool,
                    exiftool_source,
                    compressed_path,
                    motion_photo_exclusions + orientation_args + colors_args + text_block_args,
                )
            final_buffer = compressed_path.read_bytes()
            final_tags = read_metadata(exiftool, compressed_path)
            final_transparency = pixel_transparency(compressed_path)
            same_picture = same_displayed_picture(exiftool_source, compressed_path) if oriented else None
            colors_kept = same_colors(exiftool_source, compressed_path) if same_picture is not False else None
            if original.stat is not None:
                os.utime(compressed_path, ns=(original.stat.st_atime_ns, original.stat.st_mtime_ns))
            return final_buffer, final_tags, final_transparency, same_picture, colors_kept

        output_buffer = appended_buffer if appended_buffer is not None else compressed_buffer
        original_transparency = pixel_transparency(exiftool_source)
        if output_path is not None:
            final_buffer, final_tags, final_transparency, same_picture, colors_kept = write_atomically(
                output_path,
                output_buffer,
                apply_metadata_and_dates,
            )
        else:
            with tempfile.TemporaryDirectory() as tmpdir:
                tmp_compressed = Path(tmpdir) / "compressed"
                tmp_compressed.write_bytes(output_buffer)
                final_buffer, final_tags, final_transparency, same_picture, colors_kept = apply_metadata_and_dates(
                    tmp_compressed
                )
            sys.stdout.buffer.write(final_buffer)

    output_mtime = iso_mtime(output_path.stat()) if output_path is not None else None
    input_mtime = iso_mtime(original.stat) if original.stat is not None else None
    if converted and metadata == "keep":
        expected_losses = rewritten_text_blocks(original_tags, final_tags) + expected_losses
    metadata_diff = diff_metadata(
        original_tags,
        final_tags,
        expected_losses if metadata == "keep" else [*conversion_losses, ExpectedLoss("*", STRIP_REASON)],
        transparency=(original_transparency, final_transparency),
        mtimes=(input_mtime, output_mtime) if input_mtime is not None and output_mtime is not None else None,
        same_picture=same_picture,
        same_colors=colors_kept,
    )

    output_size = len(final_buffer)
    compression_count = tinify.compression_count

    # Report on stderr
    ratio = (1 - output_size / original.size) * 100 if original.size > 0 else 0
    console.print(
        f"[bold green]Compressed:[/bold green] {original.size:,} → {output_size:,} bytes ({ratio:.1f}% reduction)"
    )
    console.print(f"[dim]API compressions used this month: {compression_count}[/dim]")
    if metadata_diff["unexpected_losses"]:
        lost = ", ".join(loss["tag"] for loss in metadata_diff["unexpected_losses"])
        console.print(f"[bold yellow]Unexpected metadata losses:[/bold yellow] {lost}", highlight=False)

    return {
        "input": {
            "path": input_file,
            "format": original.format,
            "size_bytes": original.size,
            "mtime": input_mtime,
        },
        "output": {
            "path": output,
            "format": detect_format(final_buffer[:HEADER_SIZE]),
            "size_bytes": output_size,
            "mtime": output_mtime,
        },
        "motion_photo": {**motion_photo, "video_kept": video_kept},
        "tinify": {"compressions_this_month": compression_count},
        "metadata": metadata_diff,
    }


def main(
    input_file: str | None,
    output: str | None,
    api_key: str | None,
    metadata: str,
    output_format: str | None,
    overwrite: bool,
    disable_hard_limit: bool,
    allow_motion_photo_loss: bool,
    verbose: bool,
) -> dict[str, Any]:
    """Entry point called from cli.py."""
    return compress_image(
        input_file,
        output,
        api_key,
        metadata,
        output_format,
        overwrite,
        disable_hard_limit,
        allow_motion_photo_loss,
        verbose,
    )
