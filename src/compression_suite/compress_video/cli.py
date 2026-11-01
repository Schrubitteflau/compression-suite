"""CLI command for compress-video."""

import json
import sys
from typing import Any

import typer
from rich.progress import BarColumn, Progress, TextColumn, TimeElapsedColumn

from compression_suite.compress_video.main import compress_video as export_video
from compression_suite.utils.cli import stderr_console
from compression_suite.utils.contract import SCHEMA_VERSION, run_command
from compression_suite.utils.interruptions import Interruptions

COMMAND = "compress-video"

# Below 100 while encoding: 100 is only reported once the output is in place
ENCODING_MAX_PERCENT = 99.9


class JsonProgress:
    """The progress as JSON lines on stderr, one per step, the last one at 100 % once the output is written."""

    def __init__(self) -> None:
        self.percent: float | None = None
        self.duration: float | None = None
        self.encoded = 0.0

    def update(self, encoded: float, duration: float | None) -> None:
        """Report the seconds encoded so far, never going back."""
        self.duration = duration
        if duration:
            percent = min(ENCODING_MAX_PERCENT, round(100 * encoded / duration, 1))
            self.percent = max(percent, self.percent or 0.0)
            encoded = min(encoded, duration)
        self.encoded = max(self.encoded, round(encoded, 3))
        self._write(round(encoded, 3), self.percent)

    def finish(self) -> None:
        """Report the export as complete."""
        self._write(round(self.duration, 3) if self.duration is not None else self.encoded, 100.0)

    def _write(self, encoded: float | None, percent: float | None) -> None:
        progress = {
            "encoded_seconds": encoded,
            "duration_seconds": round(self.duration, 3) if self.duration is not None else None,
            "percent": percent,
        }
        line = {"schema_version": SCHEMA_VERSION, "command": COMMAND, "progress": progress}
        sys.stderr.write(json.dumps(line) + "\n")
        sys.stderr.flush()


class HumanProgress:
    """A progress bar on stderr, in seconds of video encoded."""

    def __init__(self) -> None:
        self.bar = Progress(
            TextColumn("Encoding"),
            BarColumn(),
            TextColumn("{task.fields[seconds]}"),
            TimeElapsedColumn(),
            console=stderr_console,
        )
        self.task = self.bar.add_task("encoding", total=None, seconds="")

    def update(self, encoded: float, duration: float | None) -> None:
        """Move the bar to the seconds encoded so far."""
        encoded = min(encoded, duration) if duration else encoded
        seconds = f"{encoded:.1f}/{duration:.1f} s" if duration else f"{encoded:.1f} s"
        self.bar.update(self.task, completed=encoded, total=duration, seconds=seconds)
        if not self.bar.live.is_started:
            self.bar.start()

    def finish(self) -> None:
        """Fill the bar and stop it."""
        task = self.bar.tasks[0]
        completed = task.total if task.total is not None else task.completed
        seconds = f"{completed:.1f}/{task.total:.1f} s" if task.total is not None else f"{completed:.1f} s"
        self.bar.update(self.task, completed=completed, seconds=seconds)
        self.stop()

    def stop(self) -> None:
        """Stop the bar, leaving the terminal usable."""
        if self.bar.live.is_started:
            self.bar.stop()


def compress_video(
    input_file: str = typer.Argument(..., help="Path to the video to compress (never modified)"),
    preset: str = typer.Option(..., "--preset", help="Id of the video preset (see video-presets)"),
    crf: int | None = typer.Option(None, "--crf", help="CRF in place of the preset's own, within its bounds"),
    output: str = typer.Option(..., "--output", "-o", help="Path to the MP4 to write"),
    overwrite: bool = typer.Option(False, "--overwrite", help="Overwrite output file if it exists"),
    json_mode: bool = typer.Option(
        False, "--json", help="Write a single JSON document on stdout, and the progress as JSON lines on stderr"
    ),
) -> None:
    """
    Compress a whole video with a named preset, for storing it: MP4, HEVC and Opus (or no audio), same definition
    and frame rate, metadata and modification date of the original kept.
    """

    # In a process of its own (stdout is the process's), the handlers stay until exit; an in-process caller (CliRunner)
    # gets its own back
    interruptions = Interruptions(until_exit=sys.stdout is sys.__stdout__)

    def body() -> dict[str, Any]:
        """Run the export and return the `result` object of the JSON contract."""
        if json_mode:
            progress = JsonProgress()
            result = export_video(input_file, output, preset, crf, overwrite, progress.update, interruptions.hold)
            progress.finish()
            return result
        bar = HumanProgress()
        try:
            result = export_video(input_file, output, preset, crf, overwrite, bar.update, interruptions.hold)
            bar.finish()
        finally:
            bar.stop()
        input_size, output_size = result["input"]["size_bytes"], result["output"]["size_bytes"]
        reduction = (1 - output_size / input_size) * 100 if input_size else 0.0
        stderr_console.print(
            f"[bold green]Compressed:[/bold green] {input_size:,} → {output_size:,} bytes ({reduction:.1f}% reduction) "
            f"in {result['elapsed_seconds']:.1f} s, preset {result['preset']}, CRF {result['crf']}",
            highlight=False,
        )
        unexpected = result["metadata"]["unexpected_losses"]
        if unexpected:
            lost = ", ".join(loss["tag"] for loss in unexpected)
            stderr_console.print(
                f"[bold yellow]Unexpected metadata losses:[/bold yellow] {lost}", highlight=False, soft_wrap=True
            )
        return result

    with interruptions:  # SIGINT, SIGTERM and SIGHUP, from the first check to the envelope
        run_command(COMMAND, json_mode, body)
