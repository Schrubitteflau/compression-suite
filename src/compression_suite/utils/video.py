import json
import logging
import subprocess
import sys
from typing import Any

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
    """
    Extract video information using ffprobe.

    Args:
        filename: Path to the video file

    Returns:
        VideoInfo: Validated video information

    Raises:
        SystemExit: If ffprobe fails or video info cannot be extracted
    """
    try:
        result: subprocess.CompletedProcess[str] = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height,r_frame_rate,pix_fmt,duration,nb_frames",
             "-of", "json", filename],
            capture_output=True,
            text=True,
            check=True
        )
        info: Any = json.loads(result.stdout)
        stream: Any = info['streams'][0]

        # Parse frame rate first
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
            duration=stream['duration'],
            frame_count=stream['nb_frames'],
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
