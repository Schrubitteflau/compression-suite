"""Shared Pydantic models for metadata structure."""

from typing import List, Literal

from pydantic import BaseModel


class TimestampInfo(BaseModel):
    """Information about a frame timestamp."""
    timestamp: float
    hash: str
    image_index: int


class VideoInfo(BaseModel):
    """Original video information."""
    width: int
    height: int
    fps: float
    duration: float


class Metadata(BaseModel):
    """Metadata for extracted frames."""
    version: str
    frame_changes_count: int
    unique_images_count: int
    timestamps: List[TimestampInfo]
    format: Literal["webp", "png"]
    video_info: VideoInfo
