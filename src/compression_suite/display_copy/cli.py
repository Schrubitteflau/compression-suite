"""CLI command for display-copy."""

import sys
from typing import Any

import typer

from compression_suite.display_copy.main import display_copy as make_display_copy
from compression_suite.utils.cli import stderr_console
from compression_suite.utils.contract import run_command
from compression_suite.utils.interruptions import Interruptions


def display_copy(
    input_file: str = typer.Argument(..., help="Path to the video (never modified)"),
    output: str = typer.Option(..., "--output", "-o", help="Path to the MP4 copy to write, if one is needed"),
    overwrite: bool = typer.Option(False, "--overwrite", help="Overwrite output file if it exists"),
    json_mode: bool = typer.Option(False, "--json", help="Write a single JSON document on stdout"),
) -> None:
    """
    Tell whether a browser plays a video as is, and if not, write an H.264 copy of it, for display only.

    Played as is: H.264 8 bits 4:2:0 in MP4 or MOV, audio absent, AAC, MP3 or Opus. Anything else is copied, at the
    same definition and frame rate, displayed orientation kept, audio kept.
    """

    # In a process of its own (stdout is the process's), the handlers stay until exit: a signal after the envelope
    # leaves its exit code. An in-process caller (CliRunner) gets its own back.
    interruptions = Interruptions(until_exit=sys.stdout is sys.__stdout__)

    def body() -> dict[str, Any]:
        """Make the copy if needed and return the `result` object of the JSON contract."""
        result = make_display_copy(input_file, output, overwrite, interruptions.hold)
        duration = result["duration_seconds"]
        size = f"{result['width']}x{result['height']}, {result['fps']} fps"
        size += f", {duration:.1f} s" if duration is not None else ""
        if result["copy_needed"]:
            stderr_console.print(
                f"[bold green]Display copy:[/bold green] {result['output']['path']} ({size}; "
                f"{', '.join(result['copy_reasons'])})",
                highlight=False,
            )
        else:
            stderr_console.print(f"[bold green]Played as is, no copy needed[/bold green] ({size})", highlight=False)
        return result

    with interruptions:  # SIGINT, SIGTERM and SIGHUP, from the first check to the envelope
        run_command("display-copy", json_mode, body)
