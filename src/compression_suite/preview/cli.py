"""CLI command for preview."""

from typing import Any

import typer

from compression_suite.preview.main import write_preview
from compression_suite.utils.cli import stderr_console
from compression_suite.utils.contract import run_command


def preview(
    input_file: str = typer.Argument(..., help="Path to the HEIC or HEIF file (never modified)"),
    output: str = typer.Option(..., "--output", "-o", help="Path to the JPEG preview to write"),
    overwrite: bool = typer.Option(False, "--overwrite", help="Overwrite output file if it exists"),
    json_mode: bool = typer.Option(False, "--json", help="Write a single JSON document on stdout"),
) -> None:
    """
    Convert a HEIC or HEIF to a JPEG, for display only.

    Full resolution, orientation applied to the pixels, ICC profile embedded. An HDR photo gives its SDR base image.
    """

    def body() -> dict[str, Any]:
        """Write the preview and return the `result` object of the JSON contract."""
        result = write_preview(input_file, output, overwrite)
        stderr_console.print(
            f"[bold green]Preview:[/bold green] {result['output']} "
            f"({result['width']}x{result['height']}, {result['size_bytes']:,} bytes)",
            highlight=False,
        )
        return result

    run_command("preview", json_mode, body)
