"""CLI command for extract-unique-frames."""

import logging
from enum import Enum
from pathlib import Path

import typer
from pydantic import ValidationError
from rich.console import Console
from rich.logging import RichHandler

from compression_suite.extract_unique_frames.main import main
from compression_suite.extract_unique_frames.manual_selection import (
    ManualSelectionCancelled,
    select_comparison_exclusion_regions,
)
from compression_suite.models.geometry import PixelRectangle
from compression_suite.utils.cli import cli_error_handler

console = Console()


class OutputFormat(str, Enum):
    """Output format options."""

    MULTIFRAME_WEBP = "multiframe-webp"
    PNG = "png"


def parse_ignore_region(value: str) -> PixelRectangle:
    """Parse an X,Y,WIDTH,HEIGHT CLI value."""
    try:
        x, y, width, height = (int(part.strip()) for part in value.split(","))
    except (TypeError, ValueError):
        raise ValueError(f"Invalid --ignore-region '{value}'; expected X,Y,WIDTH,HEIGHT") from None
    try:
        return PixelRectangle(x=x, y=y, width=width, height=height)
    except ValidationError:
        raise ValueError(
            f"Invalid --ignore-region '{value}'; X/Y must be non-negative and WIDTH/HEIGHT must be positive"
        ) from None


@cli_error_handler
def extract_unique_frames(
    input_file: str = typer.Argument(..., help="Path to the input video file"),
    output_folder: str = typer.Argument(..., help="Path to the output folder"),
    output_format: OutputFormat = typer.Option(  # noqa: B008
        OutputFormat.MULTIFRAME_WEBP,
        "--output-format",
        "-f",
        help="Output format: 'multiframe-webp' (single multi-frame WebP file) or 'png' (individual PNG files)",
    ),
    no_mpdecimate: bool = typer.Option(False, "--no-mpdecimate", help="Disable mpdecimate filter (process all frames)"),
    ignore_region: list[str] | None = typer.Option(  # noqa: B008
        None,
        "--ignore-region",
        help="Exclude X,Y,WIDTH,HEIGHT source-pixel rectangle from frame comparison; repeatable",
    ),
    manual_select_ignore_region: bool = typer.Option(
        False,
        "--manual-select-ignore-region",
        help="Interactively select comparison exclusions on a representative video frame",
    ),
    selection_timestamp: float | None = typer.Option(
        None,
        "--selection-timestamp",
        help="Video timestamp in seconds to display for manual region selection (default: midpoint)",
    ),
    overwrite: bool = typer.Option(False, "--overwrite", help="Overwrite output folder if it exists and is not empty"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable verbose logging"),
) -> None:
    """
    Extract unique frames from a video recording.

    This command analyzes a video recording of frames and extracts unique frames
    using FFmpeg's mpdecimate filter followed by perceptual hashing to remove
    consecutive duplicates. Outputs frames and metadata.json to a folder.
    """
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(message)s",
        handlers=[RichHandler(console=console, rich_tracebacks=True, show_time=True)],
    )
    logger = logging.getLogger(__name__)

    # Validate input file
    input_path = Path(input_file)
    if not input_path.exists():
        console.print(f"[bold red]Error:[/bold red] Input file does not exist: {input_file}")
        raise typer.Exit(code=1)

    if not input_path.is_file():
        console.print(f"[bold red]Error:[/bold red] Input path is not a file: {input_file}")
        raise typer.Exit(code=1)

    # Validate output folder
    output_path = Path(output_folder)
    if output_path.exists() and output_path.is_dir():
        if any(output_path.iterdir()) and not overwrite:
            console.print(
                f"[bold red]Error:[/bold red] Output folder is not empty: {output_folder}\n"
                f"Use --overwrite to overwrite existing files."
            )
            raise typer.Exit(code=1)

    use_webp = output_format == OutputFormat.MULTIFRAME_WEBP
    use_mpdecimate = not no_mpdecimate
    comparison_exclusion_regions = [parse_ignore_region(value) for value in ignore_region or []]
    if manual_select_ignore_region and comparison_exclusion_regions:
        raise ValueError("--manual-select-ignore-region cannot be used with --ignore-region")
    if selection_timestamp is not None and not manual_select_ignore_region:
        raise ValueError("--selection-timestamp requires --manual-select-ignore-region")
    if manual_select_ignore_region:
        try:
            comparison_exclusion_regions = select_comparison_exclusion_regions(
                input_file,
                selection_timestamp,
            )
        except ManualSelectionCancelled:
            console.print("[yellow]Selection cancelled; no output was created.[/yellow]")
            raise typer.Exit(code=0) from None

        resolved_flags = " ".join(
            f"--ignore-region {region.x},{region.y},{region.width},{region.height}"
            for region in comparison_exclusion_regions
        )
        console.print(f"Selected comparison exclusions: {resolved_flags}")

    logger.info(f"Extracting frames from: {input_file}")
    main(
        input_file,
        output_folder,
        use_webp,
        use_mpdecimate,
        comparison_exclusion_regions,
    )
    console.print(f"\n[bold green]Success![/bold green] Frames saved to: {output_folder}")
