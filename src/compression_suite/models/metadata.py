"""Shared Pydantic models for metadata structure."""

from typing import Literal

from pydantic import BaseModel, Field

from compression_suite.models.geometry import PixelRectangle


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
    timestamps: list[TimestampInfo]
    format: Literal["webp", "png"]
    video_info: VideoInfo
    comparison_exclusion_regions: list[PixelRectangle] = Field(default_factory=list)
