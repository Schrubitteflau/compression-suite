"""Core logic for preview-portion: a few seconds of a video encoded at the exact settings of an export, rendered for
display in a browser.

Two runs of the pinned ffmpeg, the original only read:

1. the portion is encoded as the export would encode it (`VideoPreset.ffmpeg_args`, the CRF possibly replaced) into
   an anonymous scratch file, which has no name and vanishes with the process, even killed;
2. that encoded portion is decoded and rendered in H.264 for display (`display_video_args`, as the display copy),
   into the output, written atomically.

The command keeps nothing between calls: WelcomeApp chooses which portions to encode and which to keep.
"""

import json
import math
import os
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from compression_suite.compress_image.main import validate_input_file
from compression_suite.utils.contract import CommandError, ErrorCode
from compression_suite.utils.display_video import display_video_args
from compression_suite.utils.export_audio import audio_args, kept_audio
from compression_suite.utils.ffmpeg_output import anonymous_file, run_ffmpeg, write_with_ffmpeg
from compression_suite.utils.interruptions import never_held
from compression_suite.utils.pinned_tools import resolve_tool
from compression_suite.utils.video_command import (
    check_not_the_input,
    check_video_output,
    duration_field,
    elapsed_field,
    file_field,
    output_field,
)
from compression_suite.utils.video_presets import VideoPreset, resolve_preset
from compression_suite.utils.video_probe import VideoProbe, ffmpeg_input, probe_video

DEFAULT_DURATION = 3.0


def _check_seconds(name: str, value: float, positive: bool) -> None:
    """Refuse a time that isn't a finite number of seconds, at least 0 (above 0 if `positive`)."""
    if not math.isfinite(value) or value < 0 or (positive and value == 0):
        bound = "above 0" if positive else "0 or more"
        raise CommandError(
            ErrorCode.USAGE_ERROR, f"--{name} must be a number of seconds {bound}, got {value}", {name: str(value)}
        )


def _decoders(probe: VideoProbe, preset: VideoPreset) -> tuple[str, ...]:
    """The input's decoders ffmpeg runs for the encoding: an error logged by one of them makes the input damaged."""
    kept = set(kept_audio(probe, preset))
    names = [probe.video.get("codec_name")]
    names += [stream.get("codec_name") for stream in probe.audio_streams if stream["index"] in kept]
    return tuple(dict.fromkeys(name for name in names if name))


def encode_args(input_path: Path, probe: VideoProbe, preset: VideoPreset, start: float, duration: float) -> list[str]:
    """ffmpeg's arguments encoding the portion as the export does: same encoder settings, same timestamps, the
    rotation kept as a display matrix, the audio streams the export keeps, each played as the export plays it (see
    `utils.export_audio`).

    `-ss` before the input seeks accurately: the portion starts at the first frame at or after `start`, counted from
    the start of the original. `-t` keeps `duration` seconds, less at the end of the video.
    """
    audio = audio_args(input_path, probe, preset, (start, duration))
    return [
        *("-ss", f"{start:.6f}", "-noautorotate", "-i", ffmpeg_input(input_path), *audio.inputs),
        *("-t", f"{duration:.6f}", "-map", f"0:{probe.video['index']}", *audio.maps),
        *preset.ffmpeg_args(),
        *audio.options,
        *("-fps_mode", "passthrough", "-enc_time_base:v", "demux", "-map_metadata", "-1"),
    ]


def display_args(encoded: str, probe: VideoProbe, output: str) -> list[str]:
    """ffmpeg's arguments rendering the encoded portion for display: the video as the display copy renders it
    (rotation applied, HDR tone-mapped), the Opus audio copied. The portion's timeline starts at 0."""
    return [
        *("-i", encoded, "-map", "0:v", "-map", "0:a?"),
        *display_video_args(probe),
        *("-c:a", "copy", "-map_metadata", "-1", "-movflags", "+faststart", "-f", "mp4", output),
    ]


def _seconds(value: object) -> float:
    """A time of ffprobe's output, 0 when absent or `N/A`."""
    return 0.0 if value in (None, "N/A") else float(str(value))


def _encoded_timing(fd: int) -> tuple[float, float, bool]:
    """Start time, duration and whether it has a video stream, of the encoded portion in the anonymous file."""
    completed = subprocess.run(
        [str(resolve_tool("ffprobe")), "-v", "error", "-show_format", "-show_streams", "-of", "json"]
        + [f"/dev/fd/{fd}"],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        pass_fds=(fd,),
        check=False,
    )
    if completed.returncode != 0:
        reason = " / ".join(completed.stderr.strip().splitlines()[-3:])
        raise CommandError(ErrorCode.INTERNAL_ERROR, f"ffprobe can't read the encoded portion: {reason}")
    probed = json.loads(completed.stdout)
    container = probed.get("format", {})
    has_video = any(stream.get("codec_type") == "video" for stream in probed.get("streams", []))
    return _seconds(container.get("start_time")), _seconds(container.get("duration")), has_video


def preview_portion(
    input_file: str,
    output: str,
    preset_id: str,
    crf: int | None,
    start: float,
    duration: float,
    overwrite: bool,
    hold: Callable[[], None] = never_held,
) -> dict[str, Any]:
    """Encode the portion, render it for display, and return the `result` object of the JSON contract.

    `hold` is called right before the output takes its name (see `utils.interruptions`).

    Checks run before any encoding, in this order: preset and CRF, start and duration, input, output being the input,
    format, start within the video, output.
    """
    started = time.monotonic()
    preset = resolve_preset(preset_id, crf)
    _check_seconds("start", start, positive=False)
    _check_seconds("duration", duration, positive=True)
    input_path = validate_input_file(input_file)
    input_stat = input_path.stat()
    output_path = Path(output)
    check_not_the_input(input_path, output_path)
    probe = probe_video(input_path)
    if probe.duration is not None and start >= probe.duration:
        raise CommandError(
            ErrorCode.USAGE_ERROR,
            f"The portion starts at {start} s, beyond the end of the video ({probe.duration:.3f} s)",
            {"start": start, "duration": probe.duration},
        )
    output_path = check_video_output(output, overwrite)

    with anonymous_file() as fd:
        encoded = f"/dev/fd/{fd}"
        run_ffmpeg(
            [*encode_args(input_path, probe, preset, start, duration), "-f", preset.container, encoded],
            _decoders(probe, preset),
            (fd,),
            probe.container,
        )
        offset, length, has_video = _encoded_timing(fd)
        if not has_video:
            raise CommandError(
                ErrorCode.USAGE_ERROR,
                f"The portion starts at {start} s, after the last frame of the video",
                {"start": start, "duration": probe.duration},
            )
        size = os.fstat(fd).st_size
        write_with_ffmpeg(output_path, lambda path: display_args(encoded, probe, path), overwrite, hold, pass_fds=(fd,))

    return {
        "input": file_field(input_file, probe.container, input_stat),
        "output": output_field(output),
        "duration_seconds": duration_field(probe),
        "elapsed_seconds": elapsed_field(started),
        "preset": preset.id,
        "crf": preset.crf,
        "portion": {"start_seconds": round(start + offset, 3), "duration_seconds": round(length, 3)},
        "encoded": {
            "format": preset.container,
            "size_bytes": size,
            "bitrate_kbps": round(size * 8 / length / 1000, 1) if length else None,
        },
    }
