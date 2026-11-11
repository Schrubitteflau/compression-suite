"""What the three video commands the caller runs (display-copy, preview-portion, compress-video) share in their JSON
contract: the checks of their output path, and the fields of their `result` that describe the same things.

- `input`, `output`: `file_field`, `{path, format, size_bytes, mtime}`; `format` is the container
  (`VideoProbe.container` for the input, always `mp4` for an output);
- `duration_seconds`: the video's duration, `duration_field`;
- `elapsed_seconds`: the time the command took, `elapsed_field`;
- `preset`, `crf`: the preset and the CRF it encoded with (the two commands that encode).
"""

import os
import time
from pathlib import Path
from typing import Any

from compression_suite.compress_image.main import check_output, iso_mtime
from compression_suite.utils.contract import CommandError, ErrorCode
from compression_suite.utils.video_probe import VideoProbe

OUTPUT_FORMAT = "mp4"


def check_not_the_input(input_path: Path, output_path: Path) -> None:
    """Refuse an output that is the input itself: same path, hard link or symbolic link (`usage_error`)."""
    if output_path.exists() and os.path.samefile(input_path, output_path):
        raise CommandError(ErrorCode.USAGE_ERROR, f"The output is the input file: {output_path}")


def check_video_output(output: str, overwrite: bool) -> Path:
    """The output path, refused before any encoding: a folder (`usage_error` for a path ending with a slash,
    `output_exists` for an existing folder), an existing file or a dangling symbolic link without `--overwrite`."""
    if output.endswith(os.sep):
        raise CommandError(ErrorCode.USAGE_ERROR, f"Output must be a file path, not a folder: {output}")
    output_path = Path(output)
    check_output(output_path, overwrite)
    if not overwrite and output_path.is_symlink():  # exists() follows the link: a dangling one looks free
        raise CommandError(
            ErrorCode.OUTPUT_EXISTS,
            f"Output path is a symbolic link to a missing file: {output_path}. Use --overwrite to replace the link.",
        )
    return output_path


def file_field(path: str, file_format: str, stat: os.stat_result) -> dict[str, Any]:
    """The `input` or `output` field: the path as given, the container, the size and the mtime (ISO 8601, UTC)."""
    return {"path": path, "format": file_format, "size_bytes": stat.st_size, "mtime": iso_mtime(stat)}


def output_field(output: str) -> dict[str, Any]:
    """The `output` field of an MP4 just written."""
    return file_field(output, OUTPUT_FORMAT, Path(output).stat())


def duration_field(probe: VideoProbe) -> float | None:
    """The `duration_seconds` field: the video's duration, to the millisecond, None when unknown."""
    return round(probe.duration, 3) if probe.duration is not None else None


def elapsed_field(started: float) -> float:
    """The `elapsed_seconds` field, from a `time.monotonic()` taken when the command started."""
    return round(time.monotonic() - started, 1)
