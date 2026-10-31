"""The named video presets, defined here only: listed by `video-presets`, applied by every command that encodes.

A command that encodes takes a preset id and, optionally, another CRF, and asks `resolve_preset` for the settings:

    settings = resolve_preset(preset_id, crf)  # usage_error if the id is unknown, the CRF not an int in bounds
    ffmpeg_command = [ffmpeg, "-i", source, *settings.ffmpeg_args(filters), output]

`filters` are the caller's own video filters (a trim, for instance), chained before the preset's: the preset owns
the whole `-vf`, so a caller never passes its own `-vf` or `-pix_fmt`.

No preset reduces the definition or the frame rate. The output is an MP4, with Opus audio or none, in 4:2:0 that
players and galleries read: 8-bit for an 8-bit source (RGB and 4:4:4 screen recordings included), 10-bit for a
10-bit one (HDR, HLG), its colour tags kept.
"""

from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any

from compression_suite.utils.contract import CommandError, ErrorCode

# x265 refuses an odd width or height in 4:2:0: crop the last column or row, never resize.
EVEN_CROP_FILTER = "crop=trunc(iw/2)*2:trunc(ih/2)*2"
# 4:2:0 that players read; ffmpeg picks the one losing least: 10-bit for a 10-bit source, 8-bit otherwise.
PIXEL_FORMAT_FILTER = "format=yuv420p|yuv420p10le"
PRESET_FILTERS = (EVEN_CROP_FILTER, PIXEL_FORMAT_FILTER)

# The CRF values x265 accepts, for every preset: 0 is near lossless, 51 the smallest file.
CRF_MIN = 0
CRF_MAX = 51


@dataclass(frozen=True)
class AudioSettings:
    """The audio track of the output: Opus at a fixed bitrate."""

    codec: str
    bitrate_kbps: int


@dataclass(frozen=True)
class VideoPreset:
    """A named encoding setting. `audio` is None when the output keeps no audio track."""

    id: str
    label: str
    encoder: str
    speed: str
    crf: int
    audio: AudioSettings | None
    container: str = "mp4"

    def ffmpeg_args(self, filters: Sequence[str] = ()) -> list[str]:
        """The ffmpeg output options encoding the video and audio streams with these settings.

        `filters` are the caller's video filters, chained in `-vf` before the preset's own (`PRESET_FILTERS`). No
        input, output path, stream selection, metadata or other container options: those are the caller's. An x265
        stream is tagged `hvc1`, the tag Apple's players and most galleries require in an MP4.
        """
        video = ["-vf", ",".join([*filters, *PRESET_FILTERS])]
        video += ["-c:v", f"lib{self.encoder}", "-preset", self.speed, "-crf", str(self.crf)]
        if self.encoder == "x265":
            video += ["-tag:v", "hvc1"]
        if self.audio is None:
            return [*video, "-an"]
        return [*video, "-c:a", f"lib{self.audio.codec}", "-b:a", f"{self.audio.bitrate_kbps}k"]

    def to_json(self) -> dict[str, Any]:
        """The preset as `video-presets --json` lists it."""
        return {
            "id": self.id,
            "label": self.label,
            "container": self.container,
            "video": {"encoder": self.encoder, "speed": self.speed, "crf": self.crf},
            "crf_bounds": {"min": CRF_MIN, "max": CRF_MAX},
            "audio": None
            if self.audio is None
            else {"codec": self.audio.codec, "bitrate_kbps": self.audio.bitrate_kbps},
        }


_OPUS_32K = AudioSettings("opus", 32)

# In display order; the first one is the default preset.
PRESETS: tuple[VideoPreset, ...] = (
    VideoPreset("default", "Défaut", "x265", "slow", 32, _OPUS_32K),
    VideoPreset("screen", "Écran / cours", "x265", "slow", 35, _OPUS_32K),
    VideoPreset("dance", "Danse", "x265", "slow", 45, None),
)
DEFAULT_PRESET_ID = PRESETS[0].id


def resolve_preset(preset_id: str, crf: int | None = None) -> VideoPreset:
    """The preset `preset_id`, with `crf` in place of its own CRF when given.

    Raises:
        CommandError: `usage_error` if the preset is unknown, or the CRF not a plain integer (a bool, float or
            string is refused; `details.crf` is then its `repr`) or out of bounds.
    """
    presets = {preset.id: preset for preset in PRESETS}
    if preset_id not in presets:
        raise CommandError(
            ErrorCode.USAGE_ERROR,
            f"Unknown video preset: {preset_id}. Known presets: {', '.join(presets)}",
            {"preset": preset_id, "known_presets": list(presets)},
        )
    preset = presets[preset_id]
    if crf is None:
        return preset
    if type(crf) is not int:
        raise CommandError(
            ErrorCode.USAGE_ERROR,
            f"CRF must be an integer between {CRF_MIN} and {CRF_MAX}, got {crf!r}",
            {"crf": repr(crf), "crf_min": CRF_MIN, "crf_max": CRF_MAX},
        )
    if not CRF_MIN <= crf <= CRF_MAX:
        raise CommandError(
            ErrorCode.USAGE_ERROR,
            f"CRF {crf} out of bounds: it must be an integer between {CRF_MIN} and {CRF_MAX}",
            {"crf": crf, "crf_min": CRF_MIN, "crf_max": CRF_MAX},
        )
    return replace(preset, crf=crf)
