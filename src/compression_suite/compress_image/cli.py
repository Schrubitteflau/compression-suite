"""CLI command for compress-image."""

from enum import Enum
from typing import Any, Optional

import typer

from compression_suite.compress_image.main import main
from compression_suite.utils.contract import CommandError, ErrorCode, run_command


class MetadataMode(str, Enum):
    """Metadata handling mode for image compression."""
    KEEP = "keep"
    STRIP = "strip"


def compress_image(
    input_file: Optional[str] = typer.Argument(None, help="Path to input image file (omit to read from stdin)"),
    output: Optional[str] = typer.Option(None, "--output", "-o", help="Path to output file (omit to write to stdout)"),
    api_key: Optional[str] = typer.Option(
        None,
        "--api-key",
        help="Tinify API key (default: tinify_api_key in $XDG_CONFIG_HOME/compression-suite/config.toml, "
        "~/.config by default)",
    ),
    metadata: MetadataMode = typer.Option(MetadataMode.KEEP, "--metadata", help="Metadata handling: 'keep' (preserve via exiftool) or 'strip'"),
    overwrite: bool = typer.Option(False, "--overwrite", help="Overwrite output file if it exists"),
    disable_hard_limit: bool = typer.Option(False, "--disable-hard-limit", help="Bypass the 15MB input size limit"),
    allow_motion_photo_loss: bool = typer.Option(
        False, "--allow-motion-photo-loss", help="Compress a motion photo anyway, losing its video",
    ),
    json_mode: bool = typer.Option(False, "--json", help="Write a single JSON document on stdout (requires --output)"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable verbose logging"),
) -> None:
    """
    Compress an image via the Tinify API.

    Supports JPEG, PNG, WebP, and AVIF. Reads from a file or stdin,
    writes to a file or stdout. Metadata is preserved by default using exiftool.
    """
    def body() -> dict[str, Any]:
        """Run the compression and return the `result` object of the JSON contract."""
        if json_mode and output is None:
            raise CommandError(
                ErrorCode.USAGE_ERROR, "--json requires --output: stdout is reserved for the JSON document",
            )
        return main(
            input_file=input_file,
            output=output,
            api_key=api_key,
            metadata=metadata.value,
            overwrite=overwrite,
            disable_hard_limit=disable_hard_limit,
            allow_motion_photo_loss=allow_motion_photo_loss,
            verbose=verbose,
        )

    run_command("compress-image", json_mode, body)
