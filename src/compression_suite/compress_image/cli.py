"""CLI command for compress-image."""

from enum import Enum
from typing import Any

import typer

from compression_suite.compress_image.main import main
from compression_suite.utils.contract import CommandError, ErrorCode, run_command


class MetadataMode(str, Enum):
    """Metadata handling mode for image compression."""

    KEEP = "keep"
    STRIP = "strip"


class OutputFormat(str, Enum):
    """Format the compressed image is written in, when it is not the input's."""

    JPEG = "jpeg"


def compress_image(
    input_file: str | None = typer.Argument(None, help="Path to input image file (omit to read from stdin)"),
    output: str | None = typer.Option(None, "--output", "-o", help="Path to output file (omit to write to stdout)"),
    api_key: str | None = typer.Option(
        None,
        "--api-key",
        help="Tinify API key (default: tinify_api_key in $XDG_CONFIG_HOME/compression-suite/config.toml, "
        "~/.config by default)",
    ),
    metadata: MetadataMode = typer.Option(
        MetadataMode.KEEP, "--metadata", help="Metadata handling: 'keep' (preserve via exiftool) or 'strip'"
    ),
    output_format: OutputFormat | None = typer.Option(
        None,
        "--format",
        help="Write the output as 'jpeg': an opaque PNG is converted locally to JPEG, then compressed; "
        "a JPEG is compressed as is (default: the input's format)",
    ),
    overwrite: bool = typer.Option(False, "--overwrite", help="Overwrite output file if it exists"),
    disable_hard_limit: bool = typer.Option(False, "--disable-hard-limit", help="Bypass the 15MB input size limit"),
    allow_motion_photo_loss: bool = typer.Option(
        False,
        "--allow-motion-photo-loss",
        help="Compress a motion photo whose video can't be kept, losing its video",
    ),
    json_mode: bool = typer.Option(False, "--json", help="Write a single JSON document on stdout (requires --output)"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable verbose logging"),
) -> None:
    """
    Compress an image via the Tinify API.

    Supports JPEG, PNG, WebP, and AVIF. Reads from a file or stdin,
    writes to a file or stdout. Metadata is preserved by default using exiftool.

    With --format jpeg, a PNG is converted to JPEG locally, then compressed by Tinify like any JPEG: one compression,
    as for any call. A JPEG input is compressed as is; any other format is refused before Tinify. The PNG's metadata
    is copied into the JPEG, and the PNG encoding fields are reported as expected losses. A PNG with at least one
    pixel not fully opaque is refused before Tinify (transparent_input): JPEG has no transparency, and it is never
    flattened onto a background. --output is taken as given, whatever its extension.

    The conversion runs with Pillow, already a pinned dependency. Not with Tinify's own convert, which costs one more
    compression per image; not with ImageMagick, a system tool with a long history of decoder vulnerabilities. The
    JPEG handed to Tinify is encoded at quality 92 with 4:2:0 chroma subsampling, ImageMagick's default: on four opaque
    PNGs (2026-11-16), it gave finals up to 30% smaller than quality 95 4:4:4 (none on the vector illustration; photo
    260 KB against 370, Tinify's own PNG 1442), with a PSNR above 41 dB on photos and illustrations. A text screenshot
    comes out heavier and blurrier than Tinify's PNG: compress such an image without --format, or both ways and keep
    the better.
    """

    def body() -> dict[str, Any]:
        """Run the compression and return the `result` object of the JSON contract."""
        if json_mode and output is None:
            raise CommandError(
                ErrorCode.USAGE_ERROR,
                "--json requires --output: stdout is reserved for the JSON document",
            )
        return main(
            input_file=input_file,
            output=output,
            api_key=api_key,
            metadata=metadata.value,
            output_format=output_format.value if output_format is not None else None,
            overwrite=overwrite,
            disable_hard_limit=disable_hard_limit,
            allow_motion_photo_loss=allow_motion_photo_loss,
            verbose=verbose,
        )

    run_command("compress-image", json_mode, body)
