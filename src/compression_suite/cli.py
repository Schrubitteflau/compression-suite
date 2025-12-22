"""Console script for compression_suite."""

import logging
from enum import Enum
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.logging import RichHandler

from compression_suite.extract_unique_frames.main import main as extract_unique_frames_main
from compression_suite.reassemble_video.main import main as reassemble_video_main

app = typer.Typer()
console = Console()


class OutputFormat(str, Enum):
    """Output format options."""
    MULTIFRAME_WEBP = "multiframe-webp"
    PNG = "png"


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
def extract_unique_frames(
    input_file: str = typer.Argument(..., help="Path to the input video file"),
    output_folder: str = typer.Argument(..., help="Path to the output folder"),
    output_format: OutputFormat = typer.Option(OutputFormat.MULTIFRAME_WEBP, "--output-format", "-f", help="Output format: 'multiframe-webp' (single multi-frame WebP file) or 'png' (individual PNG files)"),
    no_mpdecimate: bool = typer.Option(False, "--no-mpdecimate", help="Disable mpdecimate filter (process all frames)"),
    overwrite: bool = typer.Option(False, "--overwrite", help="Overwrite output folder if it exists and is not empty"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable verbose logging"),
) -> None:
    """
    Extract unique frames from a video recording.

    This command analyzes a video recording of frames and extracts unique frames
    using FFmpeg's mpdecimate filter followed by perceptual hashing to remove
    consecutive duplicates. Outputs frames and metadata.json to a folder.
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

    # Validate output folder
    output_path = Path(output_folder)
    if output_path.exists() and output_path.is_dir():
        # Check if folder is not empty
        if any(output_path.iterdir()) and not overwrite:
            console.print(
                f"[bold red]Error:[/bold red] Output folder is not empty: {output_folder}\n"
                f"Use --overwrite to overwrite existing files."
            )
            raise typer.Exit(code=1)

    use_webp = output_format == OutputFormat.MULTIFRAME_WEBP
    use_mpdecimate = not no_mpdecimate

    try:
        logger.info(f"Extracting frames from: {input_file}")
        extract_unique_frames_main(input_file, output_folder, use_webp, use_mpdecimate)
        console.print(f"\n[bold green]✓ Success![/bold green] Frames saved to: {output_folder}")
    except KeyboardInterrupt:
        console.print("\n[bold yellow]Interrupted by user[/bold yellow]")
        raise typer.Exit(code=130)
    except Exception as e:
        logger.exception("An error occurred during processing")
        console.print(f"\n[bold red]✗ Failed:[/bold red] {e}")
        raise typer.Exit(code=1)


@app.command()
def reassemble_video(
    frames_folder: str = typer.Argument(..., help="Path to folder containing extracted frames and metadata.json"),
    output_file: str = typer.Argument(..., help="Path to output video file (e.g., output.mp4)"),
    audio_file: Optional[str] = typer.Option(None, "--audio", "-a", help="Path to audio file to include in the video"),
    video_codec: str = typer.Option("libx264", "--codec", "-c", help="FFmpeg video codec (default: libx264)"),
    crf: int = typer.Option(23, "--crf", help="Constant Rate Factor for quality (0-51, lower is better, default: 23)"),
    preset: str = typer.Option("medium", "--preset", "-p", help="Encoding preset (ultrafast/superfast/veryfast/faster/fast/medium/slow/slower/veryslow)"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable verbose logging"),
) -> None:
    """
    Reassemble video from extracted unique frames.

    This command takes a folder created by extract-unique-frames (containing frames
    and metadata.json) and rebuilds the video, optionally adding an audio track.
    """
    setup_logging(verbose)
    logger = logging.getLogger(__name__)

    # Validate frames folder
    frames_path = Path(frames_folder)
    if not frames_path.exists():
        console.print(f"[bold red]Error:[/bold red] Frames folder does not exist: {frames_folder}")
        raise typer.Exit(code=1)

    if not frames_path.is_dir():
        console.print(f"[bold red]Error:[/bold red] Path is not a directory: {frames_folder}")
        raise typer.Exit(code=1)

    metadata_path = frames_path / "metadata.json"
    if not metadata_path.exists():
        console.print(f"[bold red]Error:[/bold red] metadata.json not found in: {frames_folder}")
        raise typer.Exit(code=1)

    # Validate audio file if provided
    if audio_file:
        audio_path = Path(audio_file)
        if not audio_path.exists():
            console.print(f"[bold red]Error:[/bold red] Audio file does not exist: {audio_file}")
            raise typer.Exit(code=1)

    try:
        logger.info(f"Reassembling video from: {frames_folder}")
        reassemble_video_main(frames_folder, output_file, audio_file, video_codec, crf, preset)
        console.print(f"\n[bold green]✓ Success![/bold green] Video saved to: {output_file}")
    except KeyboardInterrupt:
        console.print("\n[bold yellow]Interrupted by user[/bold yellow]")
        raise typer.Exit(code=130)
    except Exception as e:
        logger.exception("An error occurred during processing")
        console.print(f"\n[bold red]✗ Failed:[/bold red] {e}")
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
