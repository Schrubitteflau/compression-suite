"""The audio of an export, shared by the export (`compress-video`) and its preview portions (`preview-portion`), so
that a portion sounds like the same seconds of the export.

Every audio stream is kept, each encoded with the preset's audio settings, downmixed to a layout Opus accepts when
needed; a preset without audio drops them all.

An audio stream starting with an empty edit then one edit of media (a Galaxy's sound, starting a few milliseconds
after the picture) keeps its start and its length: ffmpeg plays such a track wrongly, moving its end by the length of
the empty edit, cutting it or making it longer. Such a stream is read from a second input that ignores the edit lists,
and its edit is played by filters: the media from its start for the duration of the edit, delayed by the empty edit.
"""

from dataclasses import dataclass
from pathlib import Path

from compression_suite.utils.quicktime_boxes import edit_lists
from compression_suite.utils.video_presets import VideoPreset, opus_layout, opus_layout_args
from compression_suite.utils.video_probe import VideoProbe, ffmpeg_input

# The delay, the media start and the duration played of an audio stream's edit, in seconds
Edit = tuple[float, float, float]


@dataclass(frozen=True)
class AudioArgs:
    """ffmpeg's arguments for the audio of an export: a second input (after the original, input 0), the maps (after
    the video's) and the output options."""

    inputs: list[str]
    maps: list[str]
    options: list[str]


def delayed_audio(path: Path, probe: VideoProbe) -> dict[int, Edit]:
    """The audio streams starting with an empty edit then one edit of media, by stream index (see
    `EditList.delayed_start`). None outside MP4, MOV and 3GP."""
    if not probe.is_quicktime:
        return {}
    edits = edit_lists(path)
    delayed = {}
    for stream in probe.audio_streams:
        edit = edits.get(probe.track_ids.get(stream["index"], -1))
        start = edit.delayed_start() if edit is not None else None
        if start is not None:
            delayed[stream["index"]] = start
    return delayed


def kept_audio(probe: VideoProbe, preset: VideoPreset) -> tuple[int, ...]:
    """The audio streams an export keeps, by index: all of them, none for a preset without audio."""
    return () if preset.audio is None else tuple(stream["index"] for stream in probe.audio_streams)


def audio_args(
    path: Path, probe: VideoProbe, preset: VideoPreset, window: tuple[float, float] | None = None
) -> AudioArgs:
    """ffmpeg's arguments encoding the audio as the export does.

    For the whole video (`window` None), every stream kept. For a portion, `window` is its start and duration, in
    seconds of the original, the original (input 0) being read from that start (`-ss` before it): a delayed stream
    is read from just before the window too, its edit placed on the portion's timeline. A delayed stream with none
    of its sound in the window stays at its place, as silence with its channels, as long as the portion (cut short
    by the end of the video): the player plays the same track as in the export.
    """
    delayed = delayed_audio(path, probe)
    streams = kept_audio(probe, preset)
    silent = [index for index in streams if window is not None and _outside(delayed.get(index), window)]
    second = [index for index in streams if index in delayed and index not in silent]
    # Where the second input is read from: the media of the delayed stream reaching the window first
    seek = 0.0
    if window is not None and second:
        seek = max(0.0, min(media + max(0.0, window[0] - delay) for delay, media, _ in (delayed[i] for i in second)))
    inputs = []
    if second:
        inputs = ["-ignore_editlist", "1", *(["-ss", f"{seek:.6f}"] if window is not None else []), "-i"]
        inputs.append(ffmpeg_input(path))
    by_index = {stream["index"]: stream for stream in probe.audio_streams}
    layouts = [(by_index[i].get("channels") or 0, by_index[i].get("channel_layout") or None) for i in streams]
    first_silence = 2 if second else 1
    if silent and window is not None:
        # No longer than what remains of the video: the last portion is cut short by its end
        remaining = window[1] if probe.duration is None else max(0.0, min(window[1], probe.duration - window[0]))
    for index in silent:
        inputs += ["-f", "lavfi", "-t", f"{remaining:.6f}", "-i"]
        channels, layout = layouts[streams.index(index)]
        inputs.append(f"anullsrc=channel_layout={opus_layout(channels, layout)}:sample_rate=48000")
    maps, options = [], []
    for position, index in enumerate(streams):
        if index in silent:
            maps += ["-map", f"{first_silence + silent.index(index)}:a"]
            continue
        maps += ["-map", f"{1 if index in delayed else 0}:{index}"]
        if index in delayed:
            options += [f"-filter:a:{position}", _edit_filter(delayed[index], window, seek)]
    options += opus_layout_args(layouts)
    return AudioArgs(inputs, maps, options)


def _outside(edit: Edit | None, window: tuple[float, float]) -> bool:
    """Whether a delayed stream's edit has none of its sound in the window (False for a stream not delayed)."""
    if edit is None:
        return False
    delay, _, duration = edit
    return delay + duration <= window[0] or delay >= window[0] + window[1]


def _edit_filter(edit: Edit, window: tuple[float, float] | None, seek: float) -> str:
    """The filters playing a delayed stream's edit, read without its edit list.

    For the whole video: its media from the start of the edit, for the duration of the edit, delayed by the empty
    edit. For a portion read from `seek` in the media (timestamps then starting at 0 there): the same media, placed
    at its time in the original less the portion's start, what falls before the portion cut off.
    """
    delay, media, duration = edit
    if window is None:
        return f"atrim=start={media:.9f}:duration={duration:.9f},asetpts=PTS-STARTPTS+{delay:.9f}/TB"
    shift = seek + delay - media - window[0]
    return (
        f"atrim=start={max(0.0, media - seek):.9f}:end={media + duration - seek:.9f},"
        f"asetpts=PTS{shift:+.9f}/TB,atrim=start=0"
    )
