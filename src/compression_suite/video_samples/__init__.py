"""Fabricated video samples shared by the video command tests (see `make_video_samples.py` and `README.md`)."""

from pathlib import Path

SAMPLES = Path(__file__).resolve().parent

HEVC_ROTATED = SAMPLES / "hevc_rotated.mp4"
H264 = SAMPLES / "h264.mp4"
HDR_PQ = SAMPLES / "hdr_pq.mp4"

# Colours of the pattern, as encoded: tests compare decoded pixels to them with a tolerance
RED = (220, 40, 40)
BLUE = (40, 40, 220)
GREEN = (40, 200, 40)
