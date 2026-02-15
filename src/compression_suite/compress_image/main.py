"""Core logic for compress-image: Tinify API compression with metadata preservation."""

import subprocess
import sys
import tempfile
from pathlib import Path

import tinify
from rich.console import Console

from compression_suite.utils.dependencies import check_exiftool, check_jpegoptim

HARD_LIMIT_BYTES = 15 * 1024 * 1024  # 15 MB

EXIFTOOL_EXCLUDE_TAGS = [
    "-ThumbnailImage=",
    "-Compression=",
]

# Tags that -all:all doesn't copy and must be listed explicitly.
EXIFTOOL_EXTRA_TAGS = [
    "-InteropIndex",
    "-InteropVersion",
]


def validate_input_file(path: str, disable_hard_limit: bool) -> Path:
    """Validate that the input file exists and is within size limits."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Input file does not exist: {path}")
    if not p.is_file():
        raise ValueError(f"Input path is not a file: {path}")
    if not disable_hard_limit and p.stat().st_size > HARD_LIMIT_BYTES:
        raise ValueError(
            f"Input file exceeds {HARD_LIMIT_BYTES // (1024 * 1024)}MB hard limit. "
            f"Use --disable-hard-limit to override."
        )
    return p


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
        raise RuntimeError(f"exiftool failed: {result.stderr.strip()}")


def compress_image(
    input_file: str | None,
    output: str | None,
    api_key: str,
    metadata: str,
    overwrite: bool,
    disable_hard_limit: bool,
    verbose: bool,
) -> None:
    """Compress an image via Tinify API with optional metadata preservation.

    Args:
        input_file: Path to input file, or None for stdin.
        output: Path to output file, or None for stdout.
        metadata: "keep" to preserve metadata via exiftool, "strip" to discard.
        overwrite: Allow overwriting existing output file.
        disable_hard_limit: Bypass the 15MB hard limit.
        verbose: Enable verbose reporting.
    """
    console = Console(stderr=True)

    exiftool_version = check_exiftool()
    jpegoptim_version = check_jpegoptim()
    if verbose:
        console.print(f"[dim]exiftool version: {exiftool_version}, jpegoptim version: {jpegoptim_version}[/dim]")

    tinify.key = api_key

    # Validate input and prepare API call (deferred to avoid burning a credit before checks)
    original_path = None
    if input_file is not None:
        original_path = validate_input_file(input_file, disable_hard_limit)
        input_size = original_path.stat().st_size
        prepare_source = lambda: tinify.from_file(str(original_path))
    else:
        buffer = sys.stdin.buffer.read()
        input_size = len(buffer)
        if not disable_hard_limit and input_size > HARD_LIMIT_BYTES:
            raise ValueError(
                f"Input exceeds {HARD_LIMIT_BYTES // (1024 * 1024)}MB hard limit. "
                f"Use --disable-hard-limit to override."
            )
        prepare_source = lambda: tinify.from_buffer(buffer)

    # Check output path before calling the API (to avoid burning an API credit)
    if output is not None:
        output_path = Path(output)
        if output_path.exists() and not overwrite:
            raise FileExistsError(
                f"Output file already exists: {output}. Use --overwrite to replace."
            )

    # Compress via API
    compressed_buffer = prepare_source().to_buffer()
    output_size = len(compressed_buffer)

    # Metadata preservation
    if metadata == "keep":
        # We need a file on disk for exiftool
        if original_path is None:
            # stdin mode: write original buffer to a temp file for exiftool source
            original_temp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
            original_temp.write(buffer)
            original_temp.close()
            exiftool_source = Path(original_temp.name)
        else:
            exiftool_source = original_path

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_compressed = Path(tmpdir) / "compressed"
            tmp_compressed.write_bytes(compressed_buffer)
            preserve_metadata(exiftool_source, tmp_compressed)
            compressed_buffer = tmp_compressed.read_bytes()

        # Clean up stdin temp file
        if original_path is None:
            exiftool_source.unlink(missing_ok=True)

    # Write output
    if output is not None:
        output_path = Path(output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(compressed_buffer)
    else:
        sys.stdout.buffer.write(compressed_buffer)

    # Report on stderr
    ratio = (1 - output_size / input_size) * 100 if input_size > 0 else 0
    console.print(f"[bold green]Compressed:[/bold green] {input_size:,} → {output_size:,} bytes ({ratio:.1f}% reduction)")
    console.print(f"[dim]API compressions used this month: {tinify.compression_count}[/dim]")


def main(
    input_file: str | None,
    output: str | None,
    api_key: str,
    metadata: str,
    overwrite: bool,
    disable_hard_limit: bool,
    verbose: bool,
) -> None:
    """Entry point called from cli.py."""
    compress_image(input_file, output, api_key, metadata, overwrite, disable_hard_limit, verbose)
