"""CLI command for preview-portion."""

import sys
from typing import Any

import typer

from compression_suite.compress_video.main import Interruptions
from compression_suite.preview_portion.main import DEFAULT_DURATION
from compression_suite.preview_portion.main import preview_portion as encode_portion
from compression_suite.utils.cli import stderr_console
from compression_suite.utils.contract import run_command


def preview_portion(
    input_file: str = typer.Argument(..., help="Path to the video (never modified)"),
    preset: str = typer.Option(..., "--preset", help="Id of the video preset (see video-presets)"),
    crf: int | None = typer.Option(None, "--crf", help="CRF in place of the preset's own, within its bounds"),
    start: float = typer.Option(..., "--start", help="Where the portion starts in the video, in seconds"),
    duration: float = typer.Option(DEFAULT_DURATION, "--duration", help="Length of the portion, in seconds"),
    output: str = typer.Option(..., "--output", "-o", help="Path to the MP4 to write, for display"),
    overwrite: bool = typer.Option(False, "--overwrite", help="Overwrite output file if it exists"),
    json_mode: bool = typer.Option(False, "--json", help="Write a single JSON document on stdout"),
) -> None:
    """
    Encode a few seconds of a video at the exact settings of a preset, as the export would, and write them in H.264
    for display in a browser.
    """
    # In a process of its own (stdout is the process's), the handlers stay until exit: a signal after the envelope
    # leaves its exit code. An in-process caller (CliRunner) gets its own back.
    interruptions = Interruptions(until_exit=sys.stdout is sys.__stdout__)

    def body() -> dict[str, Any]:
        """Encode the portion and return the `result` object of the JSON contract."""
        result = encode_portion(input_file, output, preset, crf, start, duration, overwrite)
        encoded = result["encoded"]
        stderr_console.print(
            f"[bold green]Preview portion:[/bold green] {result['output']} "
            f"({result['start_seconds']:.3f} s + {result['duration_seconds']:.3f} s, preset {result['preset']}, "
            f"CRF {result['crf']}: {encoded['size_bytes']:,} bytes, {encoded['bitrate_kbps']} kb/s, "
            f"in {result['elapsed_seconds']:.1f} s)",
            highlight=False,
        )
        return result

    with interruptions:  # SIGINT and SIGTERM, from the first check to the envelope
        run_command("preview-portion", json_mode, body)
