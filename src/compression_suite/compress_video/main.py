"""Core logic for compress-video: the whole video encoded with a named preset, to be stored in place of the original.

The original is only read. The pinned ffmpeg encodes it into a temporary file next to the output; the pinned exiftool
copies back the metadata ffmpeg can't write; the file gets the original's dates, then is renamed to the output. An
export stopped at any point (an error, SIGINT, SIGTERM) leaves neither the output nor a temporary file.
"""

import contextlib
import json
import os
import signal
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from compression_suite.compress_image.main import check_output, current_umask, iso_mtime, validate_input_file
from compression_suite.utils.contract import CommandError, ErrorCode
from compression_suite.utils.formats import UNKNOWN_FORMAT, detect_file_format, detect_file_video_container
from compression_suite.utils.metadata_diff import REENCODING_REASON, ExpectedLoss, read_metadata
from compression_suite.utils.pinned_tools import resolve_tool
from compression_suite.utils.quicktime_boxes import add_user_data_boxes, edit_lists, user_data_boxes
from compression_suite.utils.video_metadata import diff_video_metadata, match_tracks, track_groups
from compression_suite.utils.video_presets import VideoPreset, opus_layout, opus_layout_args, resolve_preset

# Called with the seconds encoded so far and the duration of the video (None when unknown)
ProgressCallback = Callable[[float, float | None], None]

# Metadata ffmpeg reads but doesn't write in an MP4, copied back by exiftool: the user data (`©xyz` GPS of Android
# phones, author), the `mdta` keys (Android version and capture frame rate, Apple's location and model), the
# iTunes-style item list and the XMP. ffmpeg keeps the rotation (a display matrix, kept with -noautorotate), the track
# languages and handler names itself.
EXIFTOOL_COPIED_GROUPS = ("-Keys:all", "-UserData:all", "-ItemList:all", "-XMP:all")

# The dates of a QuickTime movie (`mvhd`) and of each of its tracks (`tkhd`, `mdhd`). ffmpeg writes the creation date in
# all of them, modification dates included, and turns a zero date into the creation date: exiftool writes back the
# original's, track by track.
MOVIE_DATE_TAGS = ("CreateDate", "ModifyDate")
TRACK_DATE_TAGS = ("TrackCreateDate", "TrackModifyDate", "MediaCreateDate", "MediaModifyDate")
QUICKTIME_CONTAINERS = ("mp4", "mov", "3gp")

SUPPORTED_CONTAINERS = "mp4, mov, 3gp, matroska, webm, avi, mpegts"

# Losses decided by compression-suite, shown with their reason in the metadata diff. A loss found on a real video must
# first be fixed by a copy (EXIFTOOL_COPIED_GROUPS, quicktime_dates): only a loss that can't be avoided belongs here.
# The encoding parameters, which the export chooses anew: brands, sizes and offsets, time scales, codec, encoder, bit
# rates, sample rate. The durations of the media (`mdhd`) count the delay of x265's B-frames and of Opus's priming,
# that the edit list removes: the duration of the movie and of each track (`mvhd`, `tkhd`) is still compared.
REENCODING_LOSSES = [
    ExpectedLoss(pattern, REENCODING_REASON)
    for pattern in (
        "QuickTime:MajorBrand",
        "QuickTime:MinorVersion",
        "QuickTime:CompatibleBrands",
        "QuickTime:MediaDataSize",
        "QuickTime:MediaDataOffset",
        "QuickTime:TimeScale",
        "Track*:MediaTimeScale",
        "Track*:MediaDuration",
        "Track*:CompressorID",
        "Track*:CompressorName",
        "Track*:BitDepth",
        "Track*:VideoFrameRate",
        "Track*:BufferSize",
        "Track*:MaxBitrate",
        "Track*:AverageBitrate",
        "Track*:AudioFormat",
        "Track*:AudioSampleRate",
        "Track*:AudioBitsPerSample",
    )
]
# Track IDs the export gives anew, numbering its tracks from 1 (a phone's may have gaps, its own data tracks among
# them): each track's, the next one free, the timecode track a video track refers to (ffmpeg writes its own, see
# `match_tracks`)
RENUMBERED_TRACKS_LOSSES = [
    ExpectedLoss(pattern, "pistes renumérotées par l'export")
    for pattern in ("Track*:TrackID", "QuickTime:NextTrackID", "Track*:TimecodeTrack")
]
# Boxes of the user data neither ffmpeg nor exiftool write, put back raw: Samsung's `smta` (the phone's model)
RAW_USER_DATA_BOXES = (b"smta",)
# When they can't be: an output whose 32-bit chunk offsets would overflow once moved by them (close to 4 GiB). Only
# the model is expected: any other Samsung tag lost would be a box of Samsung's this export doesn't know
RAW_USER_DATA_LOSS = ExpectedLoss(
    "Samsung:SamsungModel",
    "boîte Samsung « smta » : ne peut être remise, la vidéo dépasserait les 4 Go qu'adressent ses index",
)
DROPPED_TRACK_REASON = "piste non conservée : l'export ne garde que la vidéo et l'audio (données, sous-titres…)"
AUDIO_HANDLER = "Audio Track"

# Demuxers ffprobe may pick for a file that holds no video at all (text, still images)
NOT_VIDEO_DEMUXERS = ("tty", "image2", "png_pipe", "jpeg_pipe", "lavfi")

# Lines of a tool's error output kept in an error message
ERROR_LINES = 5


class Terminated(KeyboardInterrupt):
    """SIGTERM received: handled as an interruption, so that every cleanup runs."""


class Interruptions:
    """SIGINT and SIGTERM as interruptions, for the whole command, until the output is in place.

    Only the first signal interrupts: the next ones are ignored, so that the cleanup it starts runs to the end. From
    `hold()`, called right before the output takes its name, both signals are ignored: the export is then done and
    reported as such, never an `interrupted` envelope with a complete output on disk. The block must hold the writing
    of the envelope.

    With `until_exit`, both signals are ignored once the block is over, until the process exits: one arriving after
    the envelope (the interpreter shutting down) leaves the exit code of the envelope, 0 for a done export, 130 for
    an interrupted one, rather than killing the process. A Python handler wouldn't do: the interpreter puts the
    default one back for SIGINT while shutting down.
    """

    SIGNALS = (signal.SIGINT, signal.SIGTERM)

    def __init__(self, until_exit: bool) -> None:
        self.until_exit = until_exit
        self.held = False
        self.previous: dict[int, Any] = {}

    def __enter__(self) -> "Interruptions":
        for signum in self.SIGNALS:
            self.previous[signum] = signal.signal(signum, self._interrupt)
        return self

    def __exit__(self, *exc_info: object) -> None:
        for signum, handler in self.previous.items():
            signal.signal(signum, signal.SIG_IGN if self.until_exit else handler)

    def hold(self) -> None:
        """Ignore both signals from now on: the export can no longer be undone."""
        self.held = True

    def _interrupt(self, signum: int, frame: Any) -> None:
        if self.held:
            return
        self.held = True  # the next signals must not cut the cleanup this one starts
        raise Terminated() if signum == signal.SIGTERM else KeyboardInterrupt()


@dataclass(frozen=True)
class AudioTrack:
    """An audio stream of the original: its index, channels and layout (None when the file gives none)."""

    index: int
    channels: int
    layout: str | None


@dataclass(frozen=True)
class VideoSource:
    """What an export needs to know of the original, read by ffprobe."""

    container: str
    duration: float | None  # seconds, None when unknown
    video_index: int  # the video stream, a cover art (attached picture) never
    audio: tuple[AudioTrack, ...]
    track_ids: dict[int, int]  # stream index to QuickTime track ID (`tkhd`), for the streams that are tracks


def tool_path(path: Path) -> str:
    """A path as ffmpeg and ffprobe never mistake it: absolute, behind the `file:` protocol. A relative name with a
    colon before its first slash (`2024-06-01T10:30:00.mp4`, `concat:a.mp4`) would otherwise name a protocol.
    exiftool gets the absolute path alone: a name starting with a dash would otherwise be an option."""
    return f"file:{path.absolute()}"


def _last_lines(text: str) -> str:
    """The end of a tool's error output, on one line."""
    return " | ".join(line.strip() for line in text.strip().splitlines()[-ERROR_LINES:])


def _ffprobe(ffprobe: Path, path: Path) -> tuple[dict[str, Any] | None, str]:
    """ffprobe's description of a file's streams and format, or None with its errors when it can't open it."""
    probe = subprocess.run(
        [str(ffprobe), "-v", "error", "-show_streams", "-show_format", "-of", "json", tool_path(path)],
        capture_output=True,
        text=True,
    )
    return (json.loads(probe.stdout) if probe.returncode == 0 else None), probe.stderr


def _video_streams(description: dict[str, Any]) -> list[dict[str, Any]]:
    """The video streams, without the cover arts (attached pictures)."""
    return [
        stream
        for stream in description.get("streams", [])
        if stream.get("codec_type") == "video" and not stream.get("disposition", {}).get("attached_pic")
    ]


def _unsupported(message: str, detected_format: str) -> CommandError:
    return CommandError(
        ErrorCode.UNSUPPORTED_FORMAT, f"Unsupported format: {message}", {"detected_format": detected_format}
    )


def probe_video(ffprobe: Path, path: Path) -> VideoSource:
    """What the export needs to know of a video, read by ffprobe.

    Raises:
        CommandError: `unsupported_format` for an image, a file in none of the supported containers (naming the
            container of a video in another one), or without exactly one video stream besides its cover arts (audio
            only, two videos); `input_corrupt` for a video container ffprobe can't open (truncated, for instance).
    """
    image_format = detect_file_format(path)
    if image_format != UNKNOWN_FORMAT:
        raise _unsupported(f"{image_format} is not a video", image_format)
    container = detect_file_video_container(path)
    if container is None:
        description, _ = _ffprobe(ffprobe, path)
        demuxer = (description or {}).get("format", {}).get("format_name", "").split(",")[0]
        if description is not None and _video_streams(description) and demuxer not in NOT_VIDEO_DEMUXERS:
            raise _unsupported(
                f"video container {demuxer} not supported (supported containers: {SUPPORTED_CONTAINERS})", demuxer
            )
        raise _unsupported(f"not a video (supported containers: {SUPPORTED_CONTAINERS})", UNKNOWN_FORMAT)
    description, errors = _ffprobe(ffprobe, path)
    if description is None:
        raise corrupt_video_error(container, errors)
    videos = _video_streams(description)
    if not videos:
        raise _unsupported(f"this {container} file holds no video stream", container)
    if len(videos) > 1:
        raise _unsupported(f"this {container} file holds {len(videos)} video streams, only one is supported", container)
    streams = description.get("streams", [])
    audio = tuple(
        AudioTrack(stream["index"], int(stream.get("channels") or 0), stream.get("channel_layout") or None)
        for stream in streams
        if stream.get("codec_type") == "audio"
    )
    track_ids = {
        stream["index"]: int(stream["id"], 16)
        for stream in streams
        if container in QUICKTIME_CONTAINERS and str(stream.get("id", "")).startswith("0x")
    }
    duration = description.get("format", {}).get("duration") or videos[0].get("duration")
    return VideoSource(
        container=container,
        duration=float(duration) if duration not in (None, "N/A") else None,
        video_index=videos[0]["index"],
        audio=audio,
        track_ids=track_ids,
    )


def corrupt_video_error(container: str, tool_errors: str) -> CommandError:
    """The `input_corrupt` error of a video its decoder can't read to the end."""
    return CommandError(
        ErrorCode.INPUT_CORRUPT,
        f"Damaged {container} file, it can't be decoded: {_last_lines(tool_errors)}",
        {"detected_format": container},
    )


def check_not_the_input(input_path: Path, output_path: Path) -> None:
    """Refuse an output that is the original itself: the original is never modified."""
    if output_path.exists() and os.path.samefile(input_path, output_path):
        raise CommandError(ErrorCode.USAGE_ERROR, f"Output is the input file itself: {output_path}")


def output_streams(source: VideoSource, preset: VideoPreset) -> tuple[int, ...]:
    """The streams of the original the output holds, in order: the video, then every audio stream unless the preset
    keeps no audio."""
    audio = () if preset.audio is None else tuple(track.index for track in source.audio)
    return (source.video_index, *audio)


def delayed_audio(path: Path, source: VideoSource) -> dict[int, tuple[float, float, float]]:
    """The audio streams starting with an empty edit then one edit of media, by stream index: the delay, the media
    start and the duration played, in seconds (see `EditList.delayed_start`).

    ffmpeg plays such a track wrongly: it moves its end by the length of the empty edit, cutting it (a Galaxy's sound,
    starting a few milliseconds after the picture) or making it longer. The export plays their edit itself.
    """
    if source.container not in QUICKTIME_CONTAINERS:
        return {}
    edits = edit_lists(path)
    delayed = {}
    for track in source.audio:
        edit = edits.get(source.track_ids.get(track.index, -1))
        start = edit.delayed_start() if edit is not None else None
        if start is not None:
            delayed[track.index] = start
    return delayed


def ffmpeg_command(
    ffmpeg: Path,
    path: Path,
    source: VideoSource,
    destination: Path,
    preset: VideoPreset,
    delayed: dict[int, tuple[float, float, float]] | None = None,
) -> list[str]:
    """The ffmpeg command encoding the video stream and every audio stream into an MP4.

    Each audio stream is encoded with the preset's audio settings, downmixed to the nearest layout Opus accepts when
    needed; a preset without audio drops them all. The video stream is mapped by its index: a cover art may come
    first.

    - `-xerror`: stop at the first decoding error rather than encode a damaged video silently, cut short;
    - `-noautorotate`: the pixels are stored as they are, the rotation kept as metadata, as in the original;
    - `-fps_mode passthrough`, `-enc_time_base:v demux`: every frame, at its own time, in the source's time base
      (a phone's variable frame rate would otherwise be rounded to the nominal one);
    - `-map_metadata 0`: dates of the movie and of its tracks. Without its `location`, which ffmpeg would write as
      a 3GPP `loci` box, a second GPS the original doesn't have: exiftool copies the original's back;
    - `+faststart`: the index first, so that a player starts before the whole file is read.

    An audio stream of `delayed` (see `delayed_audio`) is read from a second input that ignores the edit lists, and
    its edit is played by filters: the media from its start for the duration of the edit, delayed by the empty edit.
    """
    delayed = delayed or {}
    streams = output_streams(source, preset)
    maps = [arg for index in streams for arg in ("-map", f"{1 if index in delayed else 0}:{index}")]
    second_input = ("-ignore_editlist", "1", "-i", tool_path(path)) if set(delayed) & set(streams) else ()
    audio = [index for index in streams if index != source.video_index]
    trims = []
    for position, index in enumerate(audio):
        if index in delayed:
            delay, start, duration = delayed[index]
            edit = f"atrim=start={start:.9f}:duration={duration:.9f},asetpts=PTS-STARTPTS+{delay:.9f}/TB"
            trims += [f"-filter:a:{position}", edit]
    return [
        str(ffmpeg),
        *("-nostdin", "-v", "error", "-xerror", "-nostats", "-progress", "pipe:1", "-y"),
        *("-noautorotate", "-i", tool_path(path), *second_input),
        *maps,
        *trims,
        *(
            "-map_metadata",
            "0",
            "-metadata",
            "location=",
            "-metadata",
            "location-eng=",
        ),
        *preset.ffmpeg_args(),
        *(opus_layout_args([(track.channels, track.layout) for track in source.audio]) if preset.audio else ()),
        *("-fps_mode", "passthrough", "-enc_time_base:v", "demux"),
        *("-movflags", "+faststart", "-f", preset.container, tool_path(destination)),
    ]


def encode(command: list[str], duration: float | None, on_progress: ProgressCallback) -> tuple[int, str]:
    """Run ffmpeg, reporting its progress; return its exit code and error output.

    ffmpeg runs in its own session, out of reach of a Ctrl+C sent to the terminal: an interruption reaches Python,
    which kills it before removing its file.
    """
    process = subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True
    )
    errors: list[str] = []
    reader = threading.Thread(target=lambda: errors.extend(process.stderr or []), daemon=True)
    reader.start()
    try:
        for line in process.stdout or []:
            key, _, value = line.strip().partition("=")
            if key == "out_time_us" and value.lstrip("-").isdigit():
                on_progress(max(0, int(value)) / 1_000_000, duration)
        process.wait()
    except BaseException:
        process.kill()
        process.wait()
        raise
    finally:
        reader.join()
    return process.returncode, "".join(errors)


def decoding_errors(ffmpeg: Path, path: Path, streams: tuple[int, ...]) -> str | None:
    """Decode the streams an export reads, without encoding; return ffmpeg's errors if it can't decode them to the
    end, else None.

    Any error ffmpeg reports counts, even when it exits with 0: a Matroska file whose second half is gone only gives
    "File ended prematurely". The frames keep their times in the source's time base, as in the export: rounded to the
    frame rate, those of a phone's variable frame rate collide, and the null muxer reports it as an error.
    """
    maps = [arg for index in streams for arg in ("-map", f"0:{index}")]
    check = subprocess.run(
        [str(ffmpeg), "-nostdin", "-v", "error", "-xerror", "-noautorotate", "-i", tool_path(path), *maps]
        + ["-fps_mode", "passthrough", "-enc_time_base:v", "demux", "-f", "null", "-"],
        capture_output=True,
        text=True,
    )
    return check.stderr or "ffmpeg failed without a message" if check.returncode != 0 or check.stderr.strip() else None


def quicktime_dates(exiftool: Path, path: Path, source: VideoSource, streams: tuple[int, ...]) -> list[str]:
    """exiftool assignments writing back the original's QuickTime dates: the movie's, then each output track's from
    its original track, found by its track ID (the stream index differs from the track number when a cover art
    comes first). Zero dates included. Nothing for a container without QuickTime dates (Matroska...)."""
    if source.container not in QUICKTIME_CONTAINERS:
        return []
    tags = [f"-QuickTime:{tag}" for tag in MOVIE_DATE_TAGS] + [f"-{tag}" for tag in ("TrackID", *TRACK_DATE_TAGS)]
    read = subprocess.run(
        [str(exiftool), "-j", "-G1", "-n", *tags, str(path.absolute())], capture_output=True, text=True, check=False
    )
    if read.returncode != 0:
        raise CommandError(ErrorCode.METADATA_COPY_FAILED, f"exiftool failed: {_last_lines(read.stderr)}")
    found = json.loads(read.stdout)[0]
    assignments = [
        f"-QuickTime:{tag}={found[f'QuickTime:{tag}']}" for tag in MOVIE_DATE_TAGS if f"QuickTime:{tag}" in found
    ]
    groups = {value: key.split(":")[0] for key, value in found.items() if key.endswith(":TrackID")}
    for number, stream in enumerate(streams, start=1):
        group = groups.get(source.track_ids.get(stream))
        for tag in TRACK_DATE_TAGS if group else ():
            if f"{group}:{tag}" in found:
                assignments.append(f"-Track{number}:{tag}={found[f'{group}:{tag}']}")
    return assignments


def copy_metadata(exiftool: Path, path: Path, destination: Path, dates: list[str]) -> None:
    """Copy back the metadata groups ffmpeg doesn't write (EXIFTOOL_COPIED_GROUPS), and write the original's dates."""
    command = [str(exiftool), "-q", "-q", "-TagsFromFile", str(path.absolute()), *EXIFTOOL_COPIED_GROUPS, *dates]
    command += ["-overwrite_original", str(destination.absolute())]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        raise CommandError(ErrorCode.METADATA_COPY_FAILED, f"exiftool failed: {_last_lines(result.stderr)}")


def put_back_user_data(path: Path, destination: Path, source: VideoSource) -> bool:
    """Put back raw into the output the boxes of the original's user data neither ffmpeg nor exiftool write
    (RAW_USER_DATA_BOXES), unless the output has them. False when they can't be (a 32-bit chunk offset would
    overflow): the output is left as it was, and the loss is expected (RAW_USER_DATA_LOSS).
    """
    if source.container not in QUICKTIME_CONTAINERS:
        return True
    present = {box[4:8] for box in user_data_boxes(destination, RAW_USER_DATA_BOXES)}
    missing = [box for box in user_data_boxes(path, RAW_USER_DATA_BOXES) if box[4:8] not in present]
    if not missing:
        return True
    try:
        add_user_data_boxes(destination, missing)
    except ValueError:
        return False
    return True


@contextlib.contextmanager
def temporary_output(output_path: Path) -> Iterator[Path]:
    """A temporary file next to the output, with the permissions of a plain write, always removed: `put_in_place`
    gives the output its own name.

    exiftool's own temporary file (`<file>_exiftool_tmp`) is removed with it. On a failure or an interruption, the
    output's folders created for it are removed too, if still empty.
    """
    created = []
    folder = output_path.parent.absolute()
    while not folder.exists():
        created.append(folder)  # deepest first
        folder = folder.parent
    output_path.parent.mkdir(parents=True, exist_ok=True)
    path = None
    try:
        fd, name = tempfile.mkstemp(dir=output_path.parent, prefix=f".{output_path.name}.", suffix=".tmp")
        os.close(fd)
        path = Path(name)
        path.chmod(0o666 & ~current_umask())
        yield path
    except BaseException:
        _remove_temporary(path)
        for folder in created:
            with contextlib.suppress(OSError):  # not empty: someone else wrote in it meanwhile
                folder.rmdir()
        raise
    finally:
        _remove_temporary(path)


def _remove_temporary(path: Path | None) -> None:
    """Remove a temporary output and exiftool's own temporary file."""
    if path is not None:
        path.unlink(missing_ok=True)
        Path(f"{path}_exiftool_tmp").unlink(missing_ok=True)


def put_in_place(temporary: Path, output_path: Path, overwrite: bool) -> None:
    """Give the finished file the output's name, atomically.

    Without `--overwrite`, a file that appeared at the output during the export is never replaced: the name is
    given by a hard link, which fails if it exists (`output_exists`). On a filesystem without hard links (exFAT,
    for instance), the output is checked once more right before the rename.
    """
    if overwrite:
        os.replace(temporary, output_path)
        return
    try:
        os.link(temporary, output_path)
    except FileExistsError:
        raise CommandError(
            ErrorCode.OUTPUT_EXISTS,
            f"Output file appeared during the export: {output_path}. Use --overwrite to replace.",
        ) from None
    except OSError:
        check_output(output_path, overwrite=False)
        os.replace(temporary, output_path)


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


def compress_video(
    input_file: str,
    output: str,
    preset_id: str,
    crf: int | None,
    overwrite: bool,
    on_progress: ProgressCallback,
    hold_interruptions: Callable[[], None],
) -> dict[str, Any]:
    """Encode the whole video with a preset and return the `result` object of the JSON contract.

    Checks run before any encoding, in this order: preset and CRF, pinned tools, input, format, output.
    `hold_interruptions` is called right before the output takes its name (see `Interruptions`).
    """
    started = time.monotonic()
    preset = resolve_preset(preset_id, crf)
    ffmpeg, ffprobe, exiftool = resolve_tool("ffmpeg"), resolve_tool("ffprobe"), resolve_tool("exiftool")
    input_path = validate_input_file(input_file)
    input_stat = input_path.stat()  # before any read, which may refresh the atime
    source = probe_video(ffprobe, input_path)
    container, duration = source.container, source.duration
    output_path = check_video_output(output, overwrite)
    check_not_the_input(input_path, output_path)
    streams = output_streams(source, preset)

    with temporary_output(output_path) as temporary:
        on_progress(0.0, duration)
        command = ffmpeg_command(ffmpeg, input_path, source, temporary, preset, delayed_audio(input_path, source))
        exit_code, errors = encode(command, duration, on_progress)
        # An error ffmpeg reports without failing (a truncated Matroska) is checked as a failure is
        if exit_code != 0 or errors.strip():
            decoder_errors = decoding_errors(ffmpeg, input_path, streams)
            if decoder_errors is not None:
                raise corrupt_video_error(container, decoder_errors)
            if exit_code != 0:
                raise CommandError(ErrorCode.INTERNAL_ERROR, f"ffmpeg failed: {_last_lines(errors)}")
        copy_metadata(exiftool, input_path, temporary, quicktime_dates(exiftool, input_path, source, streams))
        user_data_put_back = put_back_user_data(input_path, temporary, source)
        # Read before its dates are set: a read may refresh the atime
        before = read_metadata(exiftool, input_path)
        after = match_tracks(before, read_metadata(exiftool, temporary), [source.track_ids.get(s) for s in streams])
        # Last: exiftool rewrites the file. Giving it its name keeps the dates.
        os.utime(temporary, ns=(input_stat.st_atime_ns, input_stat.st_mtime_ns))
        hold_interruptions()
        put_in_place(temporary, output_path, overwrite)

    output_stat = output_path.stat()
    metadata = diff_video_metadata(
        before,
        after,
        export_expected_losses(before, after, source, preset, user_data_put_back),
        mtimes=(iso_mtime(input_stat), iso_mtime(output_stat)),
    )
    return {
        "input": {
            "path": input_file,
            "format": container,
            "size_bytes": input_stat.st_size,
            "mtime": iso_mtime(input_stat),
        },
        "output": {
            "path": output,
            "format": preset.container,  # the muxer is forced: whatever the output's extension
            "size_bytes": output_stat.st_size,
            "mtime": iso_mtime(output_stat),
        },
        "duration_seconds": round(duration, 3) if duration is not None else None,
        "elapsed_seconds": round(time.monotonic() - started, 1),
        "preset": preset.id,
        "crf": preset.crf,
        "metadata": metadata,
    }


def export_expected_losses(
    before: dict[str, Any],
    after: dict[str, Any],
    source: VideoSource,
    preset: VideoPreset,
    user_data_put_back: bool = True,
) -> list[ExpectedLoss]:
    """The losses an export causes on purpose, with their reason: the encoding parameters, each audio track Opus
    can't hold in its layout (downmixed), each track the export doesn't keep (`after`'s tracks named after the
    original's, see `match_tracks`), Samsung's model when its box couldn't be put back (see `put_back_user_data`),
    and the container of an original that isn't a QuickTime one."""
    losses = [] if user_data_put_back else [RAW_USER_DATA_LOSS]
    by_id = {before[f"{group}:TrackID"]: group for group in track_groups(before) if f"{group}:TrackID" in before}
    for track in source.audio if preset.audio else ():
        target, group = opus_layout(track.channels, track.layout), by_id.get(source.track_ids.get(track.index))
        if group is not None and target != track.layout:
            reason = f"Opus ne prend pas la disposition {track.layout or 'inconnue'} : piste mixée en {target}"
            losses.append(ExpectedLoss(f"{group}:AudioChannels", reason))
    kept = set(track_groups(after))
    dropped = [group for group in by_id.values() if group not in kept]
    for group in dropped:
        audio = before.get(f"{group}:HandlerType") == AUDIO_HANDLER
        reason = f"piste audio retirée par le préréglage « {preset.label} »" if audio else DROPPED_TRACK_REASON
        losses.append(ExpectedLoss(f"{group}:*", reason))
    if dropped:  # the movie lasts as long as its longest track: each kept track's own duration is still compared
        losses.append(ExpectedLoss("QuickTime:Duration", "durée du film : celle des pistes gardées"))
    losses += [*REENCODING_LOSSES, *RENUMBERED_TRACKS_LOSSES]
    if source.container not in QUICKTIME_CONTAINERS:
        losses.append(ExpectedLoss("*", f"conteneur {source.container} converti en MP4"))
    return losses
