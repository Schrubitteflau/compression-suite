"""Optional OpenCV adapter for interactively selecting comparison exclusions."""

import importlib
import math
import os
import sys
from typing import Any

import ffmpeg
import numpy as np

from compression_suite.models.geometry import PixelRectangle
from compression_suite.utils.video import get_video_info

WINDOW_NAME = "Select ignored regions"
EDGE_SNAP_TOLERANCE_PIXELS = 10


class ManualSelectionCancelled(Exception):
    """Raised when manual selection finishes without any rectangles."""


def snap_rectangle_to_frame_edges(
    region: PixelRectangle,
    frame_width: int,
    frame_height: int,
    tolerance: int = EDGE_SNAP_TOLERANCE_PIXELS,
) -> PixelRectangle:
    """Snap rectangle edges that are close to source-frame boundaries."""
    left = 0 if region.x <= tolerance else region.x
    top = 0 if region.y <= tolerance else region.y
    right_gap = frame_width - region.right
    bottom_gap = frame_height - region.bottom
    right = frame_width if 0 <= right_gap <= tolerance else region.right
    bottom = frame_height if 0 <= bottom_gap <= tolerance else region.bottom
    return PixelRectangle(
        x=left,
        y=top,
        width=right - left,
        height=bottom - top,
    )


def _load_opencv() -> Any:
    try:
        return importlib.import_module("cv2")
    except ImportError:
        raise RuntimeError(
            "Manual region selection requires the optional GUI dependency. "
            "Install it with `pip install 'compression-suite\\[gui]'`."
        ) from None


def _require_graphical_display() -> None:
    if sys.platform.startswith("linux") and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        raise RuntimeError(
            "Manual region selection requires a graphical desktop session. "
            "Use --ignore-region in headless environments."
        )


def _decode_video_frame(
    video_file: str,
    timestamp: float,
    width: int,
    height: int,
) -> np.ndarray:
    try:
        frame_bytes, _ = (
            ffmpeg.input(video_file, ss=timestamp)
            .output("pipe:", format="rawvideo", pix_fmt="bgr24", vframes=1)
            .run(capture_stdout=True, capture_stderr=True)
        )
    except ffmpeg.Error as error:
        details = error.stderr.decode(errors="replace") if error.stderr else "unknown FFmpeg error"
        raise RuntimeError(f"Could not decode the manual-selection frame: {details}") from error

    expected_size = width * height * 3
    if len(frame_bytes) != expected_size:
        raise RuntimeError(f"Could not decode a {width}x{height} frame at {timestamp:g} seconds")
    return np.frombuffer(frame_bytes, dtype=np.uint8).reshape((height, width, 3))


def select_comparison_exclusion_region(
    video_file: str,
    timestamp: float | None = None,
) -> PixelRectangle:
    """Select one fixed source-pixel rectangle on a representative video frame."""
    video_info = get_video_info(video_file)
    selected_timestamp = video_info.duration / 2 if timestamp is None else timestamp
    if not math.isfinite(selected_timestamp):
        raise ValueError("--selection-timestamp must be a finite number")
    if selected_timestamp < 0 or selected_timestamp >= video_info.duration:
        raise ValueError(
            "--selection-timestamp must be greater than or equal to 0 and less "
            f"than the video duration ({video_info.duration:g} seconds)"
        )

    _require_graphical_display()
    frame = _decode_video_frame(
        video_file,
        selected_timestamp,
        video_info.width,
        video_info.height,
    )
    cv2 = _load_opencv()

    window_created = False
    try:
        cv2.namedWindow(
            WINDOW_NAME,
            cv2.WINDOW_NORMAL | cv2.WINDOW_KEEPRATIO | cv2.WINDOW_GUI_NORMAL,
        )
        window_created = True
        rectangle = cv2.selectROI(
            WINDOW_NAME,
            frame,
            showCrosshair=True,
            fromCenter=False,
        )
    except Exception as error:
        raise RuntimeError(
            "Could not open the manual region selector. Ensure opencv-python GUI "
            "support and a graphical desktop session are available."
        ) from error
    finally:
        if window_created:
            cv2.destroyWindow(WINDOW_NAME)

    x, y, width, height = (int(value) for value in rectangle)
    if width <= 0 or height <= 0:
        raise ManualSelectionCancelled
    return snap_rectangle_to_frame_edges(
        PixelRectangle(x=x, y=y, width=width, height=height),
        video_info.width,
        video_info.height,
    )
