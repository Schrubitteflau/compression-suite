"""Core logic for compress-video: the whole video encoded with a named preset, to be stored in place of the original.

The original is only read, and probed as every video command probes it (`utils.video_probe`). The pinned ffmpeg
encodes it; the pinned exiftool copies back the metadata ffmpeg can't write; the file gets the original's dates, then
the output's name. The output is written as every video command writes it (`utils.ffmpeg_output.atomic_output`): an
export stopped at any point (an error, a signal, even SIGKILL) leaves neither the output nor a temporary file.
"""

import json
import os
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from compression_suite.compress_image.main import iso_mtime, validate_input_file
from compression_suite.utils.contract import CommandError, ErrorCode
from compression_suite.utils.export_audio import audio_args, kept_audio
from compression_suite.utils.ffmpeg_output import anonymous_file, atomic_output, run_ffmpeg
from compression_suite.utils.interruptions import never_held
from compression_suite.utils.metadata_diff import REENCODING_REASON, ExpectedLoss, read_metadata
from compression_suite.utils.pinned_tools import resolve_tool
from compression_suite.utils.quicktime_boxes import add_user_data_boxes, user_data_boxes
from compression_suite.utils.video_command import (
    check_not_the_input,
    check_video_output,
    duration_field,
    elapsed_field,
    file_field,
)
from compression_suite.utils.video_metadata import diff_video_metadata, match_tracks, track_groups
from compression_suite.utils.video_presets import VideoPreset, opus_layout, resolve_preset
from compression_suite.utils.video_probe import VideoProbe, ffmpeg_input, probe_video

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

# Lines of a tool's error output kept in an error message
ERROR_LINES = 5


def _last_lines(text: str) -> str:
    """The end of a tool's error output, on one line."""
    return " | ".join(line.strip() for line in text.strip().splitlines()[-ERROR_LINES:])


def output_streams(probe: VideoProbe, preset: VideoPreset) -> tuple[int, ...]:
    """The streams of the original the output holds, in order: the video, then every audio stream unless the preset
    keeps no audio."""
    return (probe.video["index"], *kept_audio(probe, preset))


def ffmpeg_args(path: Path, probe: VideoProbe, destination: str, preset: VideoPreset) -> list[str]:
    """ffmpeg's arguments encoding the video stream and every audio stream into an MP4 (see `utils.export_audio`).

    The video stream is mapped by its index: a cover art may come first.

    - `-xerror`: stop at the first decoding error rather than encode a damaged video silently, cut short;
    - `-noautorotate`: the pixels are stored as they are, the rotation kept as metadata, as in the original;
    - `-fps_mode passthrough`, `-enc_time_base:v demux`: every frame, at its own time, in the source's time base
      (a phone's variable frame rate would otherwise be rounded to the nominal one);
    - `-map_metadata 0`: dates of the movie and of its tracks. Without its `location`, which ffmpeg would write as
      a 3GPP `loci` box, a second GPS the original doesn't have: exiftool copies the original's back;
    - `+faststart`: the index first, so that a player starts before the whole file is read.
    """
    audio = audio_args(path, probe, preset)
    return [
        *("-xerror", "-noautorotate", "-i", ffmpeg_input(path), *audio.inputs),
        *("-map", f"0:{probe.video['index']}", *audio.maps),
        *("-map_metadata", "0", "-metadata", "location=", "-metadata", "location-eng="),
        *preset.ffmpeg_args(),
        *audio.options,
        *("-fps_mode", "passthrough", "-enc_time_base:v", "demux"),
        *("-movflags", "+faststart", "-f", preset.container, destination),
    ]


def decoders(probe: VideoProbe, preset: VideoPreset) -> tuple[str, ...]:
    """The input's decoders the export runs: an error logged by one of them makes the original damaged."""
    kept = set(kept_audio(probe, preset))
    names = [probe.video.get("codec_name")]
    names += [stream.get("codec_name") for stream in probe.audio_streams if stream["index"] in kept]
    return tuple(dict.fromkeys(name for name in names if name))


def quicktime_dates(exiftool: Path, path: Path, probe: VideoProbe, streams: tuple[int, ...]) -> list[str]:
    """exiftool assignments writing back the original's QuickTime dates: the movie's, then each output track's from
    its original track, found by its track ID (the stream index differs from the track number when a cover art
    comes first). Zero dates included. Nothing for a container without QuickTime dates (Matroska...)."""
    if not probe.is_quicktime:
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
        group = groups.get(probe.track_ids.get(stream))
        for tag in TRACK_DATE_TAGS if group else ():
            if f"{group}:{tag}" in found:
                assignments.append(f"-Track{number}:{tag}={found[f'{group}:{tag}']}")
    return assignments


def copy_metadata(exiftool: Path, path: Path, encoded: Path, destination: int, dates: list[str]) -> None:
    """Write into `destination` (an open file) the encoded output with the metadata groups ffmpeg doesn't write
    copied back from the original (EXIFTOOL_COPIED_GROUPS), and the original's dates. exiftool writes on its stdout:
    it can't rewrite in place a file that has no name."""
    command = [str(exiftool), "-q", "-q", "-TagsFromFile", str(path.absolute()), *EXIFTOOL_COPIED_GROUPS, *dates]
    command += ["-o", "-", str(encoded)]
    result = _run_tool(command, stdout=destination)
    if result.returncode != 0:
        raise CommandError(ErrorCode.METADATA_COPY_FAILED, f"exiftool failed: {_last_lines(result.stderr)}")


def _run_tool(command: list[str], stdout: int) -> subprocess.CompletedProcess:
    """Run a tool to its end, writing its output into `stdout`, or kill it and wait for it on an interruption."""
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=stdout, stderr=subprocess.PIPE, text=True)
    try:
        _, stderr = process.communicate()
    except BaseException:
        process.kill()
        process.wait()
        raise
    return subprocess.CompletedProcess(command, process.returncode, "", stderr)


def put_back_user_data(path: Path, destination: Path, probe: VideoProbe) -> bool:
    """Put back raw into the output the boxes of the original's user data neither ffmpeg nor exiftool write
    (RAW_USER_DATA_BOXES), unless the output has them. False when they can't be (a 32-bit chunk offset would
    overflow): the output is left as it was, and the loss is expected (RAW_USER_DATA_LOSS).
    """
    if not probe.is_quicktime:
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


def compress_video(
    input_file: str,
    output: str,
    preset_id: str,
    crf: int | None,
    overwrite: bool,
    on_progress: ProgressCallback,
    hold: Callable[[], None] = never_held,
) -> dict[str, Any]:
    """Encode the whole video with a preset and return the `result` object of the JSON contract.

    Checks run before any encoding, in this order: preset and CRF, pinned tools, input, output being the input,
    format, output. `hold` is called right before the output takes its name (see `utils.interruptions`).

    The output is written by `atomic_output`: ffmpeg encodes into an anonymous file of the output's folder, exiftool
    writes it again with the metadata into the output's own anonymous file, which gets the original's dates, then
    its name. Nothing is left by an interruption, even a SIGKILL.
    """
    started = time.monotonic()
    preset = resolve_preset(preset_id, crf)
    resolve_tool("ffmpeg")
    resolve_tool("ffprobe")
    exiftool = resolve_tool("exiftool")
    input_path = validate_input_file(input_file)
    input_stat = input_path.stat()  # before any read, which may refresh the atime
    check_not_the_input(input_path, Path(output))
    probe = probe_video(input_path)
    if probe.video_count > 1:
        raise CommandError(
            ErrorCode.UNSUPPORTED_FORMAT,
            f"Not a video the export can store: the file holds {probe.video_count} video streams, "
            "only one is supported",
            {"detected_format": probe.container},
        )
    duration = probe.duration
    output_path = check_video_output(output, overwrite)
    streams = output_streams(probe, preset)

    with atomic_output(output_path, overwrite, hold) as result, anonymous_file(output_path.parent) as encoded:
        on_progress(0.0, duration)
        run_ffmpeg(
            ffmpeg_args(input_path, probe, f"/dev/fd/{encoded}", preset),
            decoders(probe, preset),
            (encoded,),
            probe.container,
            on_progress=lambda seconds: on_progress(seconds, duration),
        )
        encoded_path = Path(f"/proc/{os.getpid()}/fd/{encoded}")
        copy_metadata(
            exiftool, input_path, encoded_path, result.fd, quicktime_dates(exiftool, input_path, probe, streams)
        )
        user_data_put_back = put_back_user_data(input_path, result.path, probe)
        # Read before its dates are set: a read may refresh the atime
        before = read_metadata(exiftool, input_path)
        after = match_tracks(before, read_metadata(exiftool, result.path), [probe.track_ids.get(s) for s in streams])
        # Last: the name, given by a link, keeps the dates
        os.utime(result.fd, ns=(input_stat.st_atime_ns, input_stat.st_mtime_ns))

    output_stat = output_path.stat()
    metadata = diff_video_metadata(
        before,
        after,
        export_expected_losses(before, after, probe, preset, user_data_put_back),
        mtimes=(iso_mtime(input_stat), iso_mtime(output_stat)),
    )
    return {
        "input": file_field(input_file, probe.container, input_stat),
        "output": file_field(output, preset.container, output_stat),
        "duration_seconds": duration_field(probe),
        "elapsed_seconds": elapsed_field(started),
        "preset": preset.id,
        "crf": preset.crf,
        "metadata": metadata,
    }


def export_expected_losses(
    before: dict[str, Any],
    after: dict[str, Any],
    probe: VideoProbe,
    preset: VideoPreset,
    user_data_put_back: bool = True,
) -> list[ExpectedLoss]:
    """The losses an export causes on purpose, with their reason: the encoding parameters, each audio track Opus
    can't hold in its layout (downmixed), each track the export doesn't keep (`after`'s tracks named after the
    original's, see `match_tracks`), Samsung's model when its box couldn't be put back (see `put_back_user_data`),
    and the container of an original that isn't a QuickTime one."""
    losses = [] if user_data_put_back else [RAW_USER_DATA_LOSS]
    by_id = {before[f"{group}:TrackID"]: group for group in track_groups(before) if f"{group}:TrackID" in before}
    for track in probe.audio_streams if preset.audio else ():
        layout = track.get("channel_layout") or None
        target, group = opus_layout(track.get("channels") or 0, layout), by_id.get(probe.track_ids.get(track["index"]))
        if group is not None and target != layout:
            reason = f"Opus ne prend pas la disposition {layout or 'inconnue'} : piste mixée en {target}"
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
    if not probe.is_quicktime:
        losses.append(ExpectedLoss("*", f"conteneur {probe.container} converti en MP4"))
    return losses
