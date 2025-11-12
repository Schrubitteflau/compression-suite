"""Console script for compression_suite."""

import logging
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.logging import RichHandler

from compression_suite.optimize_slides_recording.main import main as optimize_slides_recording_main

app = typer.Typer()
console = Console()


def setup_logging(verbose: bool = False):
    """Configure logging with Rich handler."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(message)s",
        handlers=[RichHandler(console=console, rich_tracebacks=True, show_time=True)],
    )


@app.command()
def version():
    """Display version information."""
    typer.echo("Compression Suite v0.1.0")
    raise typer.Exit()


@app.command()
def optimize_slides_recording(
    input_file: str = typer.Argument(..., help="Path to the input video file"),
    output_file: str = typer.Argument(..., help="Path to the output video file"),
    audio: Optional[str] = typer.Option(
        None, "--audio", "-a", help="Audio encoding: 'codec' or 'codec:bitrate' (e.g., 'aac', 'aac:128k'). If not specified, audio will be copied from source."
    ),
    force: bool = typer.Option(False, "--force", "-f", help="Overwrite output file if it exists"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable verbose logging"),
) -> None:
    """
    Optimize a slides recording by removing duplicate frames.

    This command analyzes a video recording of slides and removes consecutive
    duplicate frames, significantly reducing file size while preserving the
    original audio and timing.
    """
    setup_logging(verbose)
    logger = logging.getLogger(__name__)

    # Validate input file
    input_path = Path(input_file)
    if not input_path.exists():
        console.print(f"[bold red]Error:[/bold red] Input file does not exist: {input_file}")
        raise typer.Exit(code=1)

    if not input_path.is_file():
        console.print(f"[bold red]Error:[/bold red] Input path is not a file: {input_file}")
        raise typer.Exit(code=1)

    # Validate output file
    output_path = Path(output_file)
    if output_path.exists() and not force:
        console.print(
            f"[bold red]Error:[/bold red] Output file already exists: {output_file}\n"
            f"Use --force/-f to overwrite it."
        )
        raise typer.Exit(code=1)

    # Check if output directory exists
    if output_path.parent != Path(".") and not output_path.parent.exists():
        console.print(f"[bold red]Error:[/bold red] Output directory does not exist: {output_path.parent}")
        raise typer.Exit(code=1)

    # Parse audio parameter
    audio_codec:str|None = None
    audio_bitrate:str|None = None
    if audio:
        parts = audio.split(":", 1)
        audio_codec = parts[0]
        if len(parts) > 1:
            audio_bitrate = parts[1]

        if not audio_codec:
            console.print(f"[bold red]Error:[/bold red] Invalid audio format: {audio}")
            raise typer.Exit(code=1)

    try:
        logger.info(f"Starting optimization of: {input_file}")
        optimize_slides_recording_main(input_file, output_file, audio_codec, audio_bitrate)
        console.print(f"\n[bold green]✓ Success![/bold green] Output saved to: {output_file}")
    except KeyboardInterrupt:
        console.print("\n[bold yellow]Interrupted by user[/bold yellow]")
        raise typer.Exit(code=130)
    except Exception as e:
        logger.exception("An error occurred during processing")
        console.print(f"\n[bold red]✗ Failed:[/bold red] {e}")
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
