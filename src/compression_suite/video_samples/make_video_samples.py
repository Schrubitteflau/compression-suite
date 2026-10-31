"""Fabricate the video samples: the repo is public, so no real video (with its GPS) can be versioned.

The samples last one second, at 30 fps, and weigh about 10 KB. Their pixels show the same pattern as the HEIC
samples: red left half, blue right half, green square in the top-left corner, so a test can tell from the decoded
pixels whether a rotation was applied, and how.

- `hevc_rotated.mp4`: laid out like a Galaxy S23 portrait video. HEVC Main (8 bits, 4:2:0, tag `hvc1`) stored as a
  128x72 landscape with a display matrix of -90 degrees (ffprobe `rotation=-90`, exiftool `Rotation: 90`), so it is
  displayed as a 72x128 portrait: red top half, blue bottom half, green square top-right. AAC audio (a 440 Hz sine,
  48 kHz), brand `mp42`, a creation date (2024-06-15 10:00:00 UTC) and a fake GPS position (12.3456 N, 65.4321 E),
  written where the S23 writes them (`creation_time`, `©xyz`).
- `h264.mp4`: a video a browser plays as is: H.264 High (8 bits, 4:2:0), AAC audio, brand `isom`, no rotation.
  Displayed as stored: 128x72.
- `hdr_pq.mp4`: an HDR10 video, as the S23 records with its "HDR10+ video" option: HEVC Main 10 (`yuv420p10le`),
  tagged BT.2020 primaries and matrix, PQ transfer (`smpte2084`), in the bitstream and the container. No audio, no
  rotation, 128x72. The pattern's 8-bit colours are only tagged PQ, not converted: tests check what the copy is
  tagged and that it is not black, not its exact colours.

Each sample is encoded by the pinned ffmpeg (`just install-tools`). Run from the repo root:
`uv run python src/compression_suite/video_samples/make_video_samples.py`
"""

import subprocess
import tempfile
from pathlib import Path

from compression_suite.utils.pinned_tools import resolve_tool

SAMPLES = Path(__file__).resolve().parent

WIDTH, HEIGHT = 128, 72
DURATION = 1
FPS = 30
CREATION_TIME = "2024-06-15T10:00:00.000000Z"
FAKE_LOCATION = "+12.3456+065.4321/"  # fake GPS, ISO 6709, as in `©xyz`
DISPLAY_ROTATION = -90  # counter-clockwise degrees, as ffprobe reports a S23 portrait video

PATTERN = (
    f"color=c=0xdc2828:s={WIDTH}x{HEIGHT}:r={FPS}:d={DURATION},"
    f"drawbox=x={WIDTH // 2}:y=0:w={WIDTH // 2}:h={HEIGHT}:c=0x2828dc:t=fill,"
    "drawbox=x=0:y=0:w=16:h=16:c=0x28c828:t=fill"
)
# One encoding thread: a threaded encoder's output depends on the machine, a regenerated sample would differ
X265_ONE_THREAD = "frame-threads=1:pools=1"

SINE = f"sine=frequency=440:duration={DURATION}:sample_rate=48000"


def ffmpeg(*args: str) -> None:
    """Run the pinned ffmpeg, quietly, overwriting its output."""
    subprocess.run([str(resolve_tool("ffmpeg")), "-v", "error", "-nostdin", "-y", *args], check=True)


def make_hevc_rotated(path: Path) -> None:
    """Encode the HEVC sample, then add its display matrix: ffmpeg only sets one on an input, when copying."""
    with tempfile.TemporaryDirectory() as tmp:
        unrotated = Path(tmp) / "unrotated.mp4"
        ffmpeg(
            *("-f", "lavfi", "-i", PATTERN, "-f", "lavfi", "-i", SINE),
            *(
                "-c:v",
                "libx265",
                "-x265-params",
                f"log-level=error:{X265_ONE_THREAD}",
                "-preset",
                "ultrafast",
                "-crf",
                "30",
            ),
            *("-pix_fmt", "yuv420p", "-tag:v", "hvc1", "-c:a", "aac", "-b:a", "32k", "-shortest"),
            *("-metadata", f"creation_time={CREATION_TIME}", "-metadata", f"location={FAKE_LOCATION}"),
            *("-brand", "mp42", str(unrotated)),
        )
        ffmpeg(
            *("-display_rotation:v:0", str(DISPLAY_ROTATION), "-i", str(unrotated)),
            *("-map", "0", "-c", "copy", "-map_metadata", "0", "-brand", "mp42", str(path)),
        )


def make_h264(path: Path) -> None:
    """Encode the H.264 sample, playable as is."""
    ffmpeg(
        *("-f", "lavfi", "-i", PATTERN, "-f", "lavfi", "-i", SINE),
        *(
            "-c:v",
            "libx264",
            "-threads",
            "1",
            "-preset",
            "veryfast",
            "-crf",
            "30",
            "-profile:v",
            "high",
            "-pix_fmt",
            "yuv420p",
        ),
        *("-c:a", "aac", "-b:a", "32k", "-shortest", "-movflags", "+faststart", str(path)),
    )


def make_hdr_pq(path: Path) -> None:
    """Encode the HDR10 sample: 10 bits, tagged BT.2020 and PQ."""
    colour = "colorprim=bt2020:transfer=smpte2084:colormatrix=bt2020nc"
    ffmpeg(
        *("-f", "lavfi", "-i", PATTERN),
        *(
            "-c:v",
            "libx265",
            "-x265-params",
            f"log-level=error:{X265_ONE_THREAD}:{colour}",
            "-preset",
            "ultrafast",
            "-crf",
            "30",
        ),
        *("-pix_fmt", "yuv420p10le", "-tag:v", "hvc1"),
        *("-color_primaries", "bt2020", "-color_trc", "smpte2084", "-colorspace", "bt2020nc", str(path)),
    )


def main() -> None:
    """Write the samples next to this script."""
    make_hevc_rotated(SAMPLES / "hevc_rotated.mp4")
    make_h264(SAMPLES / "h264.mp4")
    make_hdr_pq(SAMPLES / "hdr_pq.mp4")


if __name__ == "__main__":
    main()
