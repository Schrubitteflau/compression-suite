"""Core logic for compress-image: Tinify API compression with metadata preservation."""

import os
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import tinify
from rich.console import Console
from tinify import AccountError, ClientError, ServerError
from tinify import ConnectionError as TinifyConnectionError

from compression_suite.utils.contract import CommandError, ErrorCode
from compression_suite.utils.dependencies import check_exiftool
from compression_suite.utils.formats import HEADER_SIZE, detect_file_format, detect_format

HARD_LIMIT_BYTES = 15 * 1024 * 1024  # 15 MB

SUPPORTED_FORMATS = ("jpeg", "png", "webp", "avif")

EXIFTOOL_EXCLUDE_TAGS = [
    "-ThumbnailImage=",
    "-Compression=",
]

# Tags that -all:all doesn't copy and must be listed explicitly.
EXIFTOOL_EXTRA_TAGS = [
    "-InteropIndex",
    "-InteropVersion",
]


def validate_input_file(path: str) -> Path:
    """Check that the input exists and is a regular file."""
    p = Path(path)
    if not p.is_file():
        reason = "is not a file" if p.exists() else "does not exist"
        raise CommandError(ErrorCode.INPUT_NOT_FOUND, f"Input file {reason}: {path}")
    return p


def check_format(detected_format: str) -> None:
    if detected_format not in SUPPORTED_FORMATS:
        raise CommandError(
            ErrorCode.UNSUPPORTED_FORMAT,
            f"Unsupported format: {detected_format} (supported: {', '.join(SUPPORTED_FORMATS)})",
            {"detected_format": detected_format},
        )


def check_size(size: int, disable_hard_limit: bool) -> None:
    if not disable_hard_limit and size > HARD_LIMIT_BYTES:
        raise CommandError(
            ErrorCode.FILE_TOO_LARGE,
            f"Input exceeds {HARD_LIMIT_BYTES // (1024 * 1024)}MB hard limit ({size:,} bytes). "
            f"Use --disable-hard-limit to override.",
            {"size_bytes": size, "limit_bytes": HARD_LIMIT_BYTES},
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


def preserve_metadata(original_path: Path, compressed_path: Path):
    """Copy metadata from original to compressed file using exiftool, excluding problematic tags."""
    cmd = [
        "exiftool",
        "-TagsFromFile", str(original_path),
        "-all:all",
        *EXIFTOOL_EXTRA_TAGS,
        *EXIFTOOL_EXCLUDE_TAGS,
        "-overwrite_original",
        str(compressed_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise CommandError(ErrorCode.METADATA_COPY_FAILED, f"exiftool failed: {result.stderr.strip()}")


def write_atomically(output_path: Path, data: bytes, finalize: Callable[[Path], None] | None = None) -> None:
    """Write to a temporary file in the destination folder, optionally post-process it, then rename it."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=output_path.parent, prefix=f".{output_path.name}.", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        if finalize is not None:
            finalize(tmp_path)
        os.replace(tmp_path, output_path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def compress_image(
    input_file: str | None,
    output: str | None,
    api_key: str,
    metadata: str,
    overwrite: bool,
    disable_hard_limit: bool,
    verbose: bool,
) -> dict[str, Any]:
    """Compress an image via Tinify API with optional metadata preservation.

    Every check that can fail without Tinify runs before the API call, so that no credit is wasted.

    Args:
        input_file: Path to input file, or None for stdin.
        output: Path to output file, or None for stdout.
        metadata: "keep" to preserve metadata via exiftool, "strip" to discard.
        overwrite: Allow overwriting existing output file.
        disable_hard_limit: Bypass the 15MB hard limit.
        verbose: Enable verbose reporting.

    Returns:
        The `result` object of the JSON contract.
    """
    console = Console(stderr=True)

    exiftool_version = check_exiftool()
    if verbose:
        console.print(f"[dim]exiftool version: {exiftool_version}[/dim]")

    tinify.key = api_key

    # Validate input and prepare API call (deferred to avoid burning a credit before checks)
    original_path = None
    if input_file is not None:
        original_path = validate_input_file(input_file)
        input_format = detect_file_format(original_path)
        check_format(input_format)
        input_size = original_path.stat().st_size
        check_size(input_size, disable_hard_limit)
        prepare_source = lambda: tinify.from_file(str(original_path))
    else:
        buffer = sys.stdin.buffer.read()
        input_format = detect_format(buffer[:HEADER_SIZE])
        check_format(input_format)
        input_size = len(buffer)
        check_size(input_size, disable_hard_limit)
        prepare_source = lambda: tinify.from_buffer(buffer)

    output_path = Path(output) if output is not None else None
    if output_path is not None and output_path.exists() and not overwrite:
        raise CommandError(
            ErrorCode.OUTPUT_EXISTS, f"Output file already exists: {output}. Use --overwrite to replace.",
        )

    compressed_buffer = call_tinify(prepare_source)

    # Metadata preservation needs the original on disk for exiftool
    stdin_original = None
    exiftool_source = None
    if metadata == "keep":
        if original_path is None:
            stdin_original = tempfile.NamedTemporaryFile(suffix=f".{input_format}", delete=False)
            stdin_original.write(buffer)
            stdin_original.close()
            exiftool_source = Path(stdin_original.name)
        else:
            exiftool_source = original_path

    def copy_metadata(compressed_path: Path) -> None:
        if exiftool_source is not None:
            preserve_metadata(exiftool_source, compressed_path)

    try:
        if output_path is not None:
            write_atomically(output_path, compressed_buffer, copy_metadata)
            final_buffer = output_path.read_bytes()
        else:
            with tempfile.TemporaryDirectory() as tmpdir:
                tmp_compressed = Path(tmpdir) / "compressed"
                tmp_compressed.write_bytes(compressed_buffer)
                copy_metadata(tmp_compressed)
                final_buffer = tmp_compressed.read_bytes()
            sys.stdout.buffer.write(final_buffer)
    finally:
        if stdin_original is not None:
            Path(stdin_original.name).unlink(missing_ok=True)

    output_size = len(final_buffer)
    compression_count = tinify.compression_count

    # Report on stderr
    ratio = (1 - output_size / input_size) * 100 if input_size > 0 else 0
    console.print(f"[bold green]Compressed:[/bold green] {input_size:,} → {output_size:,} bytes ({ratio:.1f}% reduction)")
    console.print(f"[dim]API compressions used this month: {compression_count}[/dim]")

    return {
        "input": {"path": input_file, "format": input_format, "size_bytes": input_size},
        "output": {
            "path": output,
            "format": detect_format(final_buffer[:HEADER_SIZE]),
            "size_bytes": output_size,
        },
        "tinify": {"compressions_this_month": compression_count},
    }


def main(
    input_file: str | None,
    output: str | None,
    api_key: str,
    metadata: str,
    overwrite: bool,
    disable_hard_limit: bool,
    verbose: bool,
) -> dict[str, Any]:
    """Entry point called from cli.py."""
    return compress_image(input_file, output, api_key, metadata, overwrite, disable_hard_limit, verbose)
