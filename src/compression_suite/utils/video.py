import logging
import sys
from typing import Any

import ffmpeg
from pydantic import BaseModel, Field, field_validator

logger: logging.Logger = logging.getLogger(__name__)


class VideoInfo(BaseModel):
    """Video information extracted from ffprobe."""

    width: int = Field(..., gt=0, description="Video width in pixels")
    height: int = Field(..., gt=0, description="Video height in pixels")
    pix_fmt: str = Field(..., description="Pixel format (e.g., yuv420p)")
    fps: float = Field(..., gt=0, description="Frames per second")
    duration: float = Field(..., ge=0, description="Duration in seconds")
    frame_count: int = Field(..., ge=0, description="Total number of frames")


def get_video_info(filename: str) -> VideoInfo:
    try:
        # Use ffmpeg.probe to get video information
        probe = ffmpeg.probe(filename, select_streams='v:0')
        stream: Any = probe['streams'][0]

        # Parse frame rate
        fps_str = stream['r_frame_rate']
        if isinstance(fps_str, str) and '/' in fps_str:
            num, denom = map(int, fps_str.split('/'))
            fps_value = num / denom
        else:
            fps_value = float(fps_str)

        video_info = VideoInfo(
            width=stream['width'],
            height=stream['height'],
            pix_fmt=stream['pix_fmt'],
            fps=fps_value,
            duration=float(stream['duration']),
            frame_count=int(stream['nb_frames']),
        )

        duration_str = f", {video_info.duration:.2f}s" if video_info.duration > 0 else ""
        frames_str = f", {video_info.frame_count} frames" if video_info.frame_count > 0 else ""
        logger.info(
            f"Video detected: {video_info.width}x{video_info.height}, "
            f"{video_info.pix_fmt}, {video_info.fps:.2f} fps{duration_str}{frames_str}"
        )
        return video_info
    except Exception as e:
        logger.error(f"Error detecting video parameters: {e}")
        sys.exit(1)
