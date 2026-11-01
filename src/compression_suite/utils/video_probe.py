"""Read a video with the pinned ffprobe, and sort out what is not a video from a video that can't be decoded.

Shared by the video commands: they all start from `probe_video`, which raises the contract's errors:

- `unsupported_format`: an image (detected on the content: a HEIC opens in ffmpeg's MP4 demuxer), a file ffprobe
  doesn't recognise and whose first bytes are not those of a video container, a file without a video stream (audio
  only, cover art only), or a format ffmpeg reads as images or text (`image2`, `*_pipe`, `tty`).
- `input_corrupt`: a file whose first bytes are those of a video container but ffprobe can't open, a container
  holding no stream at all (an MP4 cut before its `moov` box, which a phone writes last), a video whose first frame
  can't be decoded, or whose decoded frames stop short of the end its container announces (an MP4 cut in its data,
  its index first). Only the first frame and the last seconds are decoded, so that a long video is probed in a
  moment: damage in the middle is only met by a command that decodes everything.

Both carry `details.detected_format`: the image format (`jpeg`, `heic`...), else the video container, named as
`VideoProbe.container` names it, else `unknown`. Any container ffprobe reads is accepted, if it holds a video stream.

Every path handed to ffmpeg or ffprobe goes through `ffmpeg_input`: a file named `concat:x.mov` is that file, never
ffmpeg's `concat` protocol reading `x.mov`.
"""

import json
import re
import subprocess
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any

from compression_suite.utils.contract import CommandError, ErrorCode
from compression_suite.utils.formats import HEADER_SIZE, UNKNOWN_FORMAT, detect_format, detect_video_container
from compression_suite.utils.pinned_tools import resolve_tool

# ffmpeg demuxers that read images or text, not videos
IMAGE_DEMUXERS = ("image2", "tty", "lavfi")
IMAGE_DEMUXER_SUFFIX = "_pipe"

# Bytes read to recognise a video container: three MPEG-TS packets
CONTAINER_HEADER_SIZE = 1024
TS_PACKET_SIZE = 188

# Seconds decoded before the end of the video stream, to check that its frames reach it: enough to hold a keyframe
END_CHECK_WINDOW = 3.0
# Frames the decoded end may fall short of the announced one by, before the file counts as truncated
END_CHECK_TOLERANCE_FRAMES = 2
DEFAULT_FRAME_DURATION = 1 / 30

# Matroska's `DURATION` tag, `HH:MM:SS.nnnnnnnnn`; any other form is ignored, as if absent
MATROSKA_DURATION = re.compile(r"(\d+):(\d{2}):(\d{2}(?:\.\d+)?)")

QUICKTIME_CONTAINERS = ("mp4", "mov", "3gp")

# Transfer characteristics of HDR video: PQ (HDR10, HDR10+, Dolby Vision) and HLG
HDR_TRANSFERS = ("smpte2084", "arib-std-b67")


@dataclass(frozen=True)
class VideoProbe:
    """What the video commands need to know about a video file."""

    container: str  # `mp4`, `mov`, `3gp`, `matroska`, `webm`, `avi`, `mpegts`, else ffprobe's first demuxer name
    format_name: str  # ffprobe's demuxer list, `mov,mp4,m4a,3gp,3g2,mj2` for MP4 and MOV
    major_brand: str | None  # ISO BMFF brand (`isom`, `mp42`, `qt  `...), None outside MP4 and MOV
    duration: float | None  # seconds, None when the container doesn't say (a Matroska written to a pipe)
    video: dict[str, Any]  # ffprobe's first video stream, never a cover art
    video_count: int  # video streams, cover arts excepted
    audio_streams: tuple[dict[str, Any], ...]  # ffprobe's audio streams, in order
    track_ids: dict[int, int]  # stream index to QuickTime track ID (`tkhd`), for MP4, MOV and 3GP

    @property
    def audio(self) -> dict[str, Any] | None:
        """ffprobe's first audio stream, None without audio."""
        return self.audio_streams[0] if self.audio_streams else None

    @property
    def is_quicktime(self) -> bool:
        """Whether the container is a QuickTime one (MP4, MOV, 3GP): edit lists, QuickTime dates, user data."""
        return self.container in QUICKTIME_CONTAINERS

    @property
    def hdr_transfer(self) -> str | None:
        """The HDR transfer of the video (`smpte2084` for PQ, `arib-std-b67` for HLG), or None for SDR."""
        transfer = self.video.get("color_transfer")
        return transfer if transfer in HDR_TRANSFERS else None

    @property
    def rotation(self) -> int:
        """Counter-clockwise display rotation in degrees (ffprobe's convention: -90 for a phone portrait), 0 if
        none."""
        for side_data in self.video.get("side_data_list", []):
            if "rotation" in side_data:
                return int(side_data["rotation"])
        return 0

    @property
    def displayed_size(self) -> tuple[int, int]:
        """Width and height as shown, rotation applied."""
        width, height = self.video["width"], self.video["height"]
        return (height, width) if self.rotation % 180 else (width, height)

    @property
    def frame_duration(self) -> float:
        """Duration of one frame at the nominal rate, in seconds."""
        numerator, _, denominator = str(self.video.get("r_frame_rate", "0/0")).partition("/")
        if denominator and int(denominator) and int(numerator):
            return int(denominator) / int(numerator)
        return DEFAULT_FRAME_DURATION

    @property
    def fps(self) -> float:
        """Average frame rate, rounded to the thousandth; the nominal rate if the average is unknown."""
        for key in ("avg_frame_rate", "r_frame_rate"):
            numerator, _, denominator = str(self.video.get(key, "0/0")).partition("/")
            if denominator and int(denominator) and int(numerator):
                return round(int(numerator) / int(denominator), 3)
        return 0.0


def looks_like_video_container(header: bytes) -> bool:
    """Whether the first bytes are those of a video container: ISO BMFF `ftyp` (MP4, MOV, 3GP), Matroska and WebM,
    AVI, MPEG-TS. Images with an `ftyp` box (HEIC, AVIF) are told apart before."""
    return (
        header[4:8] in (b"ftyp", b"moov", b"mdat", b"wide", b"free")
        or header.startswith(b"\x1a\x45\xdf\xa3")
        or (header[:4] == b"RIFF" and header[8:12] == b"AVI ")
        or (
            len(header) > 2 * TS_PACKET_SIZE
            and header[0] == header[TS_PACKET_SIZE] == header[2 * TS_PACKET_SIZE] == 0x47
        )
    )


def _run(command: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(command, capture_output=True, text=True, stdin=subprocess.DEVNULL)


def _last_lines(text: str, count: int = 3) -> str:
    return " / ".join(line.strip() for line in text.strip().splitlines()[-count:])


def corrupt_error(detected_format: str, reason: str) -> CommandError:
    """The `input_corrupt` error of a video that can't be decoded, with the reason, as every video command gives it."""
    return CommandError(
        ErrorCode.INPUT_CORRUPT,
        f"The video can't be decoded, the file is damaged: {reason}",
        {"detected_format": detected_format},
    )


def unsupported_error(detected_format: str, reason: str) -> CommandError:
    """The `unsupported_format` error of a file that is not a video, as every video command gives it."""
    return CommandError(ErrorCode.UNSUPPORTED_FORMAT, f"Not a video: {reason}", {"detected_format": detected_format})


def ffmpeg_input(path: Path) -> str:
    """A path as ffmpeg and ffprobe must be given it: with the `file:` protocol, so that a name like `concat:x.mov`
    or `subfile,,...:x.mov` is read as the file it names, not as a protocol."""
    return f"file:{path}"


def _decoded_frames(ffmpeg: Path, path: Path, stream_index: int, args: list[str]) -> tuple[int, float | None, str]:
    """Decode a video stream (`args` before `-i`: a seek; after it: a frame count) keeping its timestamps.

    Returns how many frames came out, the end of the last one in the stream's own time (its timestamp plus its
    duration, in seconds), and ffmpeg's error output if it failed. Errors that ffmpeg recovers from (concealment,
    references missing after a seek in the middle of a group of pictures) don't count: only the frames do.
    """
    before, after = (args, []) if "-ss" in args else ([], args)
    completed = _run(
        [str(ffmpeg), "-v", "error", "-nostdin", *before, "-i", ffmpeg_input(path), "-map", f"0:{stream_index}"]
        + [*after, "-copyts", "-f", "framemd5", "-"]
    )
    time_base = Fraction(1)
    count, end = 0, None
    for line in completed.stdout.splitlines():
        if line.startswith("#tb 0:"):
            time_base = Fraction(line.split(":", 1)[1].strip())
        elif line and not line.startswith("#"):
            fields = [field.strip() for field in line.split(",")]
            count += 1
            frame_end = float((int(fields[2]) + int(fields[3])) * time_base)
            end = frame_end if end is None else max(end, frame_end)
    failure = "" if completed.returncode == 0 else (_last_lines(completed.stderr) or "ffmpeg failed")
    return count, end, failure


def _announced_end(video: dict[str, Any], container_start: float) -> float | None:
    """Where the video stream ends, in its own time, as its container announces it: its start plus its duration
    (MP4, MPEG-TS), or its `DURATION` tag, which Matroska writes as the stream's end time. None if the container
    gives no duration of its own to the video stream (the container's covers the audio too), or a tag that doesn't
    parse."""
    if video.get("duration") not in (None, "N/A"):
        return float(video.get("start_time") or container_start) + float(video["duration"])
    match = MATROSKA_DURATION.fullmatch(str((video.get("tags") or {}).get("DURATION", "")).strip())
    if match:
        return int(match[1]) * 3600 + int(match[2]) * 60 + float(match[3])
    return None  # absent, or in a form that doesn't parse ("6.0", "00:00:06,000"): ignored


def _check_end(ffmpeg: Path, path: Path, probe: "VideoProbe", container_start: float) -> str | None:
    """Decode the last seconds of the video stream and check its frames reach the end the container announces for
    the video stream itself, whatever the audio lasts. None if they do, else the reason. Without a duration of the
    video stream's own, frames only have to come out.

    The seek lands on a keyframe in an MP4, anywhere in an MPEG-TS: if no frame comes out of the window (a group of
    pictures longer than it), the whole stream is decoded instead.
    """
    video = probe.video
    expected_end = _announced_end(video, container_start)
    end_of_window = expected_end if expected_end is not None else container_start + (probe.duration or 0.0)
    window_start = end_of_window - container_start - END_CHECK_WINDOW  # ffmpeg's -ss counts from the container start
    count, end, failure = 0, None, ""
    if window_start > 0:
        count, end, failure = _decoded_frames(ffmpeg, path, video["index"], ["-ss", f"{window_start:.3f}"])
    if count == 0 and not failure:
        count, end, failure = _decoded_frames(ffmpeg, path, video["index"], [])
    if failure:
        return failure
    if end is None:
        return "no frame decoded at the end of the video"
    if expected_end is not None and end < expected_end - END_CHECK_TOLERANCE_FRAMES * probe.frame_duration - 0.001:
        return (
            f"the video stops at {end - container_start:.3f} s of the {expected_end - container_start:.3f} s its "
            "container announces: the file is truncated"
        )
    return None


def probe_video(path: Path) -> VideoProbe:
    """Read a video's container and streams, decode its first frame and check its last seconds.

    Raises:
        CommandError: `unsupported_format` if the file is not a video, `input_corrupt` if it is one but can't be
            decoded, `tool_missing` if the pinned ffprobe or ffmpeg is not usable.
    """
    with path.open("rb") as f:
        header = f.read(CONTAINER_HEADER_SIZE)
    image_format = detect_format(header[:HEADER_SIZE])
    if image_format != UNKNOWN_FORMAT:
        raise unsupported_error(image_format, f"the file is an image ({image_format})")
    header_container = detect_video_container(header)

    ffprobe = resolve_tool("ffprobe")
    ffmpeg = resolve_tool("ffmpeg")
    completed = _run([str(ffprobe), "-v", "error", "-show_format", "-show_streams", "-of", "json", ffmpeg_input(path)])
    if completed.returncode != 0:
        reason = _last_lines(completed.stderr) or f"ffprobe exited with code {completed.returncode}"
        if looks_like_video_container(header):
            raise corrupt_error(header_container or UNKNOWN_FORMAT, reason)
        raise unsupported_error(UNKNOWN_FORMAT, f"ffprobe doesn't recognise the file ({reason})")

    probed = json.loads(completed.stdout)
    container = probed.get("format", {})
    format_name = container.get("format_name", UNKNOWN_FORMAT)
    if format_name in IMAGE_DEMUXERS or format_name.endswith(IMAGE_DEMUXER_SUFFIX):
        raise unsupported_error(UNKNOWN_FORMAT, f"ffmpeg reads the file as an image or text ({format_name})")
    name = header_container or format_name.split(",")[0]
    streams = probed.get("streams", [])
    if not streams:  # ffprobe opens an MP4 cut before its index without an error, and finds nothing in it
        raise corrupt_error(name, "the container holds no stream: its index is missing, the file is truncated")
    videos = [s for s in streams if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")]
    if not videos:
        raise unsupported_error(name, "the file has no video stream")

    probe = VideoProbe(
        container=name,
        format_name=format_name,
        major_brand=container.get("tags", {}).get("major_brand"),
        duration=_duration(container.get("duration"), videos[0].get("duration")),
        video=videos[0],
        video_count=len(videos),
        audio_streams=tuple(s for s in streams if s.get("codec_type") == "audio"),
        track_ids={
            s["index"]: int(s["id"], 16)
            for s in streams
            if name in QUICKTIME_CONTAINERS and str(s.get("id", "")).startswith("0x")
        },
    )
    count, _, failure = _decoded_frames(ffmpeg, path, videos[0]["index"], ["-frames:v", "1"])
    if failure or count == 0:
        raise corrupt_error(name, failure or "the first frame can't be decoded")
    end_error = _check_end(ffmpeg, path, probe, float(container.get("start_time") or 0))
    if end_error is not None:
        raise corrupt_error(name, end_error)
    return probe


def _duration(*candidates: object) -> float | None:
    """The first duration ffprobe gives, the container's before the stream's; None if neither does."""
    for value in candidates:
        if value not in (None, "", "N/A"):
            return float(str(value))
    return None
