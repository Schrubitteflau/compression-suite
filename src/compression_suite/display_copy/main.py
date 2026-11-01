"""Core logic for display-copy: an H.264 copy of a video, for display in a browser only, made only when needed.

The original is only read, never modified. It is served as is when a browser plays it: see `copy_reasons` for the
rule. Otherwise the copy is written atomically by `write_display_copy`.
"""

import os
from pathlib import Path
from typing import Any

from compression_suite.compress_image.main import check_output, validate_input_file
from compression_suite.utils.contract import CommandError, ErrorCode
from compression_suite.utils.display_video import display_video_args
from compression_suite.utils.ffmpeg_output import write_with_ffmpeg
from compression_suite.utils.video_probe import VideoProbe, ffmpeg_input, probe_video

# The rule, deliberately narrow: in doubt, a copy is made.
PLAYABLE_FORMAT = "mov,mp4,m4a,3gp,3g2,mj2"  # ffprobe's name for the ISO BMFF demuxer
MOV_BRAND = "qt  "
MP4_BRANDS = ("isom", "iso2", "iso4", "iso5", "iso6", "mp41", "mp42", "avc1", "M4V ")
PLAYABLE_VIDEO_CODEC = "h264"
PLAYABLE_PIX_FMTS = ("yuv420p", "yuvj420p")  # 8 bits 4:2:0, limited or full range
PLAYABLE_H264_PROFILES = ("Constrained Baseline", "Baseline", "Main", "High")
PLAYABLE_AUDIO_CODECS = ("aac", "mp3", "opus")

# The audio, when a browser can't play it
AAC_ARGS = ("-c:a", "aac", "-b:a", "192k")


def container_reason(probe: VideoProbe) -> str | None:
    """Why the container isn't played as is, or None for MP4 and MOV."""
    if probe.format_name != PLAYABLE_FORMAT:
        return f"container {probe.format_name}"
    if probe.major_brand != MOV_BRAND and probe.major_brand not in MP4_BRANDS:
        return f"MP4 brand {probe.major_brand!r}"
    return None


def video_reason(probe: VideoProbe) -> str | None:
    """Why the video stream isn't played as is, or None for H.264 8 bits 4:2:0."""
    video = probe.video
    if video.get("codec_name") != PLAYABLE_VIDEO_CODEC:
        return f"video codec {video.get('codec_name')}"
    if video.get("pix_fmt") not in PLAYABLE_PIX_FMTS:
        return f"pixel format {video.get('pix_fmt')}"
    if video.get("profile") not in PLAYABLE_H264_PROFILES:
        return f"H.264 profile {video.get('profile')}"
    return None


def hdr_reason(probe: VideoProbe) -> str | None:
    """Why an HDR video isn't shown as is (washed out colours), or None for SDR."""
    return f"hdr {probe.hdr_transfer}" if probe.hdr_transfer else None


def video_copied(probe: VideoProbe) -> bool:
    """Whether the video stream goes into the copy as is: the container or the audio was at fault."""
    return video_reason(probe) is None and hdr_reason(probe) is None


def audio_reason(probe: VideoProbe) -> str | None:
    """Why the audio stream isn't played as is, or None without audio or with AAC, MP3 or Opus."""
    if probe.audio is not None and probe.audio.get("codec_name") not in PLAYABLE_AUDIO_CODECS:
        return f"audio codec {probe.audio.get('codec_name')}"
    return None


def copy_reasons(probe: VideoProbe) -> list[str]:
    """Why a browser may not play the video as is; empty when it plays it."""
    reasons = (container_reason(probe), video_reason(probe), hdr_reason(probe), audio_reason(probe))
    return [reason for reason in reasons if reason]


def display_copy_args(input_path: Path, probe: VideoProbe, output: str) -> list[str]:
    """ffmpeg's arguments for the display copy: the probed video stream (never cover art) and the first audio
    stream, each copied as is when a browser plays it (only the container or the other stream was at fault), else
    re-encoded: the video as `display_video_args` renders it, the audio to AAC.

    A copied video stream keeps its display matrix and its timestamps, which browsers apply. A copied stream is
    decoded all the same, into a second output that writes nothing (`-f null`), so that damage in its middle is met
    as in a re-encoded one. The metadata is not copied: the copy is for display only. `+faststart` puts the index
    first, so a browser starts at once.
    """
    video_map = ["-map", f"0:{probe.video['index']}"]
    audio_map = ["-map", f"0:{probe.audio['index']}"] if probe.audio is not None else []
    audio_copied = probe.audio is not None and audio_reason(probe) is None
    video = ["-c:v", "copy"] if video_copied(probe) else display_video_args(probe)
    audio = [*audio_map, *(["-c:a", "copy"] if audio_copied else AAC_ARGS)] if audio_map else []
    copied = [*(video_map if video_copied(probe) else []), *(audio_map if audio_copied else [])]
    decode_only = [*copied, "-f", "null", "-"] if copied else []
    return [
        *("-i", ffmpeg_input(input_path), *video_map, *video, *audio),
        *("-map_metadata", "-1", "-movflags", "+faststart", "-f", "mp4", output),
        *decode_only,
    ]


def decoders(probe: VideoProbe) -> tuple[str, ...]:
    """The input's decoders ffmpeg runs for the copy: every stream's, the copied ones being decoded too."""
    names = [probe.video.get("codec_name")]
    if probe.audio is not None:
        names.append(probe.audio.get("codec_name"))
    return tuple(name for name in names if name)


def write_display_copy(input_path: Path, probe: VideoProbe, output_path: Path) -> None:
    """Write the display copy of a probed video, atomically."""
    write_with_ffmpeg(output_path, lambda output: display_copy_args(input_path, probe, output), decoders(probe))


def check_not_the_input(input_path: Path, output_path: Path) -> None:
    """Refuse an output that is the input itself: same path, hard link or symbolic link (`usage_error`)."""
    if output_path.exists() and os.path.samefile(input_path, output_path):
        raise CommandError(ErrorCode.USAGE_ERROR, f"The output is the input file: {output_path}")


def display_copy(input_file: str, output: str, overwrite: bool) -> dict[str, Any]:
    """Write the display copy of a video if a browser can't play it, and return the `result` of the JSON contract."""
    input_path = validate_input_file(input_file)
    check_not_the_input(input_path, Path(output))
    probe = probe_video(input_path)
    reasons = copy_reasons(probe)
    width, height = probe.displayed_size
    result: dict[str, Any] = {
        "input": input_file,
        "copy_needed": bool(reasons),
        "copy_reasons": reasons,
        "duration": probe.duration,
        "width": width,
        "height": height,
        "fps": probe.fps,
        "video_codec": probe.video.get("codec_name"),
        "audio_codec": probe.audio.get("codec_name") if probe.audio else None,
    }
    if reasons:
        output_path = Path(output)
        check_output(output_path, overwrite)
        write_display_copy(input_path, probe, output_path)
        result["output"] = output
    return result
