"""CLI command for reassemble-video."""

import logging
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.logging import RichHandler

from compression_suite.reassemble_video.main import main
from compression_suite.utils.cli import cli_error_handler

console = Console()


@cli_error_handler
def reassemble_video(
    frames_folder: str = typer.Argument(..., help="Path to folder containing extracted frames and metadata.json"),
    output_file: str = typer.Argument(..., help="Path to output video file (e.g., output.mp4)"),
    audio_file: Optional[str] = typer.Option(None, "--audio", "-a", help="Path to audio file to include in the video"),
    video_codec: str = typer.Option("libx264", "--codec", "-c", help="FFmpeg video codec (default: libx264)"),
    crf: int = typer.Option(23, "--crf", help="Constant Rate Factor for quality (0-51, lower is better, default: 23)"),
    preset: str = typer.Option("medium", "--preset", "-p", help="Encoding preset (ultrafast/superfast/veryfast/faster/fast/medium/slow/slower/veryslow)"),
    mode: str = typer.Option("vfr", "--mode", "-m", help="Frame rate mode: 'vfr' (variable, most accurate) or 'cfr' (constant, better player compatibility)"),
    fps: float = typer.Option(25.0, "--fps", help="Target framerate for CFR mode (default: 25.0)"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable verbose logging"),
) -> None:
    """
    Reassemble video from extracted unique frames.

    This command takes a folder created by extract-unique-frames (containing frames
    and metadata.json) and rebuilds the video, optionally adding an audio track.

    Use --mode vfr (default) for pixel-perfect reconstruction with variable framerate,
    or --mode cfr with --fps to force a constant framerate for better player compatibility.
    """
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(message)s",
        handlers=[RichHandler(console=console, rich_tracebacks=True, show_time=True)],
    )
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

    # Validate mode parameter
    if mode != "vfr" and mode != "cfr":
        console.print(f"[bold red]Error:[/bold red] Invalid mode '{mode}'. Must be 'vfr' or 'cfr'")
        raise typer.Exit(code=1)

    logger.info(f"Reassembling video from: {frames_folder}")
    main(frames_folder, output_file, audio_file, video_codec, crf, preset, mode, fps)
    console.print(f"\n[bold green]Success![/bold green] Video saved to: {output_file}")
