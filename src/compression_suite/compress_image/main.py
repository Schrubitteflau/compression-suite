"""Core logic for compress-image: Tinify API compression with metadata preservation."""

import os
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, TypeVar

import tinify
from rich.console import Console
from tinify import AccountError, ClientError, ServerError
from tinify import ConnectionError as TinifyConnectionError

from compression_suite.utils.config import resolve_api_key
from compression_suite.utils.contract import CommandError, ErrorCode
from compression_suite.utils.formats import HEADER_SIZE, detect_file_format, detect_format
from compression_suite.utils.metadata_diff import (
    REENCODING_REASON,
    ExpectedLoss,
    diff_metadata,
    pixel_transparency,
    read_metadata,
)
from compression_suite.utils.motion_photo import detect_motion_photo
from compression_suite.utils.pinned_tools import resolve_tool

T = TypeVar("T")

HARD_LIMIT_BYTES = 15 * 1024 * 1024  # 15 MB

SUPPORTED_FORMATS = ("jpeg", "png", "webp", "avif")

THUMBNAIL_REASON = "miniature EXIF retirée exprès : elle montrerait l'image d'origine"
SAMSUNG_TRAILER_REASON = "zone Samsung de fin de fichier : retirée par Tinify, exiftool ne sait pas la réécrire"
STRIP_REASON = "métadonnées retirées à la demande"

# Tags of exiftool's Samsung::Trailer table, read in the SEFT trailer. Their group, `Samsung`, is also the one
# of Samsung's EXIF maker notes, which are copied back: `Samsung:*` would hide their loss.
SAMSUNG_TRAILER_TAGS = (
    "DepthMapData", "DepthMapData2", "DepthMapName", "DualCameraImage", "DualCameraImageName", "EmbeddedAudioFile",
    "EmbeddedAudioFileName", "EmbeddedImage", "EmbeddedImage2", "EmbeddedImageName", "EmbeddedVideoFile",
    "EmbeddedVideoOffsetSize", "EmbeddedVideoType", "MCCData", "MotionPhotoAutoPlayVideo", "OriginalPathHashKey",
    "RemasterInfo", "SamsungCaptureInfo", "SamsungMotionPhotoVersion", "SingleShotDepthMap", "SurroundShotVideo",
    "SurroundShotVideoName", "TimeStamp", "VideoEditedTimeZone",
)

# Losses decided by compression-suite, shown with their reason in the metadata diff. Those with an
# `excluded_tag` are what EXIFTOOL_EXCLUDE_TAGS keeps from being copied back, so both can't diverge.
# A loss found on a real sample must first be fixed by a copy (EXIFTOOL_EXTRA_TAGS): only a loss
# that can't be avoided belongs here.
EXPECTED_LOSSES = [
    ExpectedLoss("IFD1:ThumbnailImage", THUMBNAIL_REASON, excluded_tag="ThumbnailImage"),
    ExpectedLoss("IFD1:Compression", "propre à la miniature EXIF, retirée avec elle", excluded_tag="Compression"),
    ExpectedLoss("IFD1:*", THUMBNAIL_REASON),  # size, offset and length of the removed thumbnail
    *(ExpectedLoss(f"Samsung:{tag}", SAMSUNG_TRAILER_REASON) for tag in SAMSUNG_TRAILER_TAGS),
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

MOTION_PHOTO_REASON = "photo animée : perte autorisée"

# Losses of a motion photo compressed with --allow-motion-photo-loss, which come before EXPECTED_LOSSES (the Samsung
# video entry is also a trailer field). The XMP declaring the video is not copied back: the compressed file would
# still claim a video it no longer has. Its toolkit, copied back with the rest, is dropped if nothing else remains.
MOTION_PHOTO_LOSSES = [
    ExpectedLoss("XMP-GCamera:MotionPhoto*", MOTION_PHOTO_REASON, excluded_tag="XMP-GCamera:MotionPhoto*"),
    ExpectedLoss("XMP-GCamera:MicroVideo*", MOTION_PHOTO_REASON, excluded_tag="XMP-GCamera:MicroVideo*"),
    ExpectedLoss("XMP-GContainer:*", MOTION_PHOTO_REASON, excluded_tag="XMP-GContainer:all"),
    ExpectedLoss("XMP-x:XMPToolkit", MOTION_PHOTO_REASON),
    ExpectedLoss("Google:MotionPhotoVideo", MOTION_PHOTO_REASON),
    ExpectedLoss("Samsung:EmbeddedVideo*", MOTION_PHOTO_REASON),
]

EXIFTOOL_MOTION_PHOTO_EXCLUDE_TAGS = [
    f"-{loss.excluded_tag}=" for loss in MOTION_PHOTO_LOSSES if loss.excluded_tag
]

# Tags that -all:all doesn't copy and must be listed explicitly (ICC_Profile and the IFD0 dimensions are
# "unsafe" for exiftool: without them, the colour profile of a P3 photo would be lost). Without XMPToolkit,
# exiftool would name itself as the toolkit of the XMP it rewrites.
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


def check_format(detected_format: str) -> None:
    """Reject any format Tinify can't compress."""
    if detected_format not in SUPPORTED_FORMATS:
        raise CommandError(
            ErrorCode.UNSUPPORTED_FORMAT,
            f"Unsupported format: {detected_format} (supported: {', '.join(SUPPORTED_FORMATS)})",
            {"detected_format": detected_format},
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


def check_motion_photo(motion_photo: dict[str, Any], allow_motion_photo_loss: bool) -> None:
    """Reject a motion photo, whose video Tinify would drop, unless its loss is allowed."""
    if motion_photo["detected"] and not allow_motion_photo_loss:
        raise CommandError(
            ErrorCode.MOTION_PHOTO,
            f"Motion photo detected ({', '.join(motion_photo['signals'])}): compressing it would drop its video. "
            f"Use --allow-motion-photo-loss to compress it anyway.",
            {"signals": motion_photo["signals"]},
        )


def call_tinify(prepare_source: Callable[[], Any]) -> bytes:
    """Run the Tinify compression, translating its exceptions into stable error codes."""
    try:
        return prepare_source().to_buffer()
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


def preserve_metadata(exiftool: Path, original_path: Path, compressed_path: Path, extra_exclusions: list[str]):
    """Copy metadata from original to compressed file using exiftool, excluding problematic tags and extra ones."""
    cmd = [
        str(exiftool),
        "-TagsFromFile", str(original_path),
        "-all:all",
        *EXIFTOOL_EXTRA_TAGS,
        *EXIFTOOL_EXCLUDE_TAGS,
        *extra_exclusions,
        "-overwrite_original",
        str(compressed_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise CommandError(ErrorCode.METADATA_COPY_FAILED, f"exiftool failed: {result.stderr.strip()}")


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
        overwrite: Allow overwriting existing output file.
        disable_hard_limit: Bypass the 15MB hard limit.
        allow_motion_photo_loss: Compress a motion photo anyway, losing its video.
        verbose: Enable verbose reporting.

    Returns:
        The `result` object of the JSON contract.
    """
    console = Console(stderr=True)

    exiftool = resolve_tool("exiftool")
    if verbose:
        console.print(f"[dim]exiftool: {exiftool}[/dim]")

    # Validate input and prepare API call (deferred to avoid burning a credit before checks)
    original_path = None
    original_stat = None
    if input_file is not None:
        original_path = validate_input_file(input_file)
        original_stat = original_path.stat()  # before any read, which may refresh the atime
        input_format = detect_file_format(original_path)
        check_format(input_format)
        input_size = original_stat.st_size
        check_size(input_size, disable_hard_limit)
        original_buffer = original_path.read_bytes()
        prepare_source = lambda: tinify.from_file(str(original_path))
    else:
        original_buffer = sys.stdin.buffer.read()
        input_format = detect_format(original_buffer[:HEADER_SIZE])
        check_format(input_format)
        input_size = len(original_buffer)
        check_size(input_size, disable_hard_limit)
        prepare_source = lambda: tinify.from_buffer(original_buffer)

    with file_on_disk(original_path, original_buffer, f".{input_format}") as exiftool_source:
        original_tags = read_metadata(exiftool, exiftool_source)
        motion_photo = detect_motion_photo(original_buffer, original_tags)
        check_motion_photo(motion_photo, allow_motion_photo_loss)
        # Past the check, a motion photo is one whose loss is allowed
        motion_photo_exclusions = EXIFTOOL_MOTION_PHOTO_EXCLUDE_TAGS if motion_photo["detected"] else []
        expected_losses = (MOTION_PHOTO_LOSSES if motion_photo["detected"] else []) + EXPECTED_LOSSES

        output_path = Path(output) if output is not None else None
        if output_path is not None and output_path.is_dir():
            raise CommandError(ErrorCode.OUTPUT_EXISTS, f"Output path is a directory: {output}.")
        if output_path is not None and output_path.exists() and not overwrite:
            raise CommandError(
                ErrorCode.OUTPUT_EXISTS, f"Output file already exists: {output}. Use --overwrite to replace.",
            )

        tinify.key = resolve_api_key(api_key)

        compressed_buffer = call_tinify(prepare_source)

        def apply_metadata_and_dates(compressed_path: Path) -> tuple[bytes, dict[str, Any], bool | None]:
            """Copy the metadata, read the final bytes, tags and transparency, then give the file the original's dates.

            The dates come last: exiftool rewrites the file, and reading it refreshes its atime.
            """
            if metadata == "keep":
                preserve_metadata(exiftool, exiftool_source, compressed_path, motion_photo_exclusions)
            final_buffer = compressed_path.read_bytes()
            final_tags = read_metadata(exiftool, compressed_path)
            final_transparency = pixel_transparency(compressed_path)
            if original_stat is not None:
                os.utime(compressed_path, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
            return final_buffer, final_tags, final_transparency

        original_transparency = pixel_transparency(exiftool_source)
        if output_path is not None:
            final_buffer, final_tags, final_transparency = write_atomically(
                output_path, compressed_buffer, apply_metadata_and_dates,
            )
        else:
            with tempfile.TemporaryDirectory() as tmpdir:
                tmp_compressed = Path(tmpdir) / "compressed"
                tmp_compressed.write_bytes(compressed_buffer)
                final_buffer, final_tags, final_transparency = apply_metadata_and_dates(tmp_compressed)
            sys.stdout.buffer.write(final_buffer)

    output_mtime = iso_mtime(output_path.stat()) if output_path is not None else None
    input_mtime = iso_mtime(original_stat) if original_stat is not None else None
    metadata_diff = diff_metadata(
        original_tags,
        final_tags,
        expected_losses if metadata == "keep" else [ExpectedLoss("*", STRIP_REASON)],
        transparency=(original_transparency, final_transparency),
        mtimes=(input_mtime, output_mtime) if input_mtime is not None and output_mtime is not None else None,
    )

    output_size = len(final_buffer)
    compression_count = tinify.compression_count

    # Report on stderr
    ratio = (1 - output_size / input_size) * 100 if input_size > 0 else 0
    console.print(f"[bold green]Compressed:[/bold green] {input_size:,} → {output_size:,} bytes ({ratio:.1f}% reduction)")
    console.print(f"[dim]API compressions used this month: {compression_count}[/dim]")
    if metadata_diff["unexpected_losses"]:
        lost = ", ".join(loss["tag"] for loss in metadata_diff["unexpected_losses"])
        console.print(f"[bold yellow]Unexpected metadata losses:[/bold yellow] {lost}", highlight=False)

    return {
        "input": {
            "path": input_file,
            "format": input_format,
            "size_bytes": input_size,
            "mtime": input_mtime,
        },
        "output": {
            "path": output,
            "format": detect_format(final_buffer[:HEADER_SIZE]),
            "size_bytes": output_size,
            "mtime": output_mtime,
        },
        "motion_photo": motion_photo,
        "tinify": {"compressions_this_month": compression_count},
        "metadata": metadata_diff,
    }


def main(
    input_file: str | None,
    output: str | None,
    api_key: str | None,
    metadata: str,
    overwrite: bool,
    disable_hard_limit: bool,
    allow_motion_photo_loss: bool,
    verbose: bool,
) -> dict[str, Any]:
    """Entry point called from cli.py."""
    return compress_image(
        input_file, output, api_key, metadata, overwrite, disable_hard_limit, allow_motion_photo_loss, verbose,
    )
