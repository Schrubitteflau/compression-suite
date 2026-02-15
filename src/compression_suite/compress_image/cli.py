"""CLI command for compress-image."""

from enum import Enum
from typing import Optional

import tinify
import typer

from compression_suite.compress_image.main import main
from compression_suite.utils.cli import cli_error_handler, stderr_console


class MetadataMode(str, Enum):
    """Metadata handling mode for image compression."""
    KEEP = "keep"
    STRIP = "strip"


@cli_error_handler
def compress_image(
    input_file: Optional[str] = typer.Argument(None, help="Path to input image file (omit to read from stdin)"),
    output: Optional[str] = typer.Option(None, "--output", "-o", help="Path to output file (omit to write to stdout)"),
    api_key: str = typer.Option(..., "--api-key", help="Tinify API key"),
    metadata: MetadataMode = typer.Option(MetadataMode.KEEP, "--metadata", help="Metadata handling: 'keep' (preserve via exiftool) or 'strip'"),
    overwrite: bool = typer.Option(False, "--overwrite", help="Overwrite output file if it exists"),
    disable_hard_limit: bool = typer.Option(False, "--disable-hard-limit", help="Bypass the 15MB input size limit"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable verbose logging"),
) -> None:
    """
    Compress an image via the Tinify API.

    Supports JPEG, PNG, WebP, and AVIF. Reads from a file or stdin,
    writes to a file or stdout. Metadata is preserved by default using exiftool.
    """
    try:
        main(
            input_file=input_file,
            output=output,
            api_key=api_key,
            metadata=metadata.value,
            overwrite=overwrite,
            disable_hard_limit=disable_hard_limit,
            verbose=verbose,
        )
    except tinify.AccountError as e:
        stderr_console.print(f"[bold red]API account error:[/bold red] {e}")
        raise typer.Exit(code=12 if "limit" in str(e).lower() else 11)
    except tinify.ClientError as e:
        stderr_console.print(f"[bold red]API client error:[/bold red] {e}")
        raise typer.Exit(code=11)
    except tinify.ServerError as e:
        stderr_console.print(f"[bold red]API server error:[/bold red] {e}")
        raise typer.Exit(code=10)
    except tinify.ConnectionError as e:
        stderr_console.print(f"[bold red]Connection error:[/bold red] {e}")
        raise typer.Exit(code=10)
