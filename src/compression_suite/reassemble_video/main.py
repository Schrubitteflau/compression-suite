#!/usr/bin/env python3
"""
Reassemble video from extracted unique frames
"""

import logging
import os
import tempfile
from pathlib import Path
from typing import List, Literal

import ffmpeg
from PIL import Image
from pydantic import BaseModel, Field
from rich.console import Console
from rich.progress import Progress, TimeElapsedColumn, BarColumn, TextColumn

logger = logging.getLogger(__name__)

# Type definitions
FrameMode = Literal["vfr", "cfr"]


# Pydantic models for metadata validation
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


def load_metadata(folder_path: Path) -> Metadata:
    """Load and validate metadata.json from the folder."""
    metadata_path = folder_path / "metadata.json"
    if not metadata_path.exists():
        raise FileNotFoundError(f"metadata.json not found in {folder_path}")

    with open(metadata_path, 'r') as f:
        data = f.read()

    return Metadata.model_validate_json(data)


def load_frames_from_webp(webp_path: Path) -> List[Image.Image]:
    """Load all frames from a multi-frame WebP file."""
    frames = []
    img = Image.open(webp_path)

    try:
        for i in range(100):
            frames.append(img.copy())
            img.seek(i + 1)
    except EOFError:
        pass  # End of frames

    return frames


def load_frames_from_pngs(folder_path: Path, metadata: Metadata) -> List[Image.Image]:
    """Load frames from individual PNG files based on metadata."""
    frames = []

    # Get unique image hashes in order
    seen_indices = set()
    image_hashes = []

    for ts in metadata.timestamps:
        idx = ts.image_index
        if idx not in seen_indices:
            seen_indices.add(idx)
            image_hashes.append(ts.hash)

    # Load images
    for hash_val in image_hashes:
        png_path = folder_path / f"{hash_val}.png"
        if not png_path.exists():
            raise FileNotFoundError(f"PNG file not found: {png_path}")
        frames.append(Image.open(png_path))

    return frames


def prepare_frames_vfr(
    temp_path: Path,
    frames: List[Image.Image],
    timestamps: List[TimestampInfo],
    video_duration: float,
    progress,
    task
) -> Path:
    """Prepare frames for VFR mode using concat demuxer."""
    concat_file = temp_path / "concat.txt"

    with open(concat_file, 'w') as f:
        for i, ts_info in enumerate(timestamps):
            # Calculate duration (time until next frame, or end of video)
            if i < len(timestamps) - 1:
                duration = timestamps[i + 1].timestamp - ts_info.timestamp
            else:
                duration = video_duration - ts_info.timestamp

            # Get the image and save it
            image_idx = ts_info.image_index
            frame = frames[image_idx]
            frame_path = temp_path / f"frame_{i:06d}.png"
            frame.save(frame_path, format='PNG')

            # Write to concat file
            f.write(f"file '{frame_path}'\n")
            f.write(f"duration {duration}\n")

            progress.update(task, advance=1)

        # Concat demuxer requires the last file to be listed again without duration
        if timestamps:
            last_frame_path = temp_path / f"frame_{len(timestamps)-1:06d}.png"
            f.write(f"file '{last_frame_path}'\n")

    return concat_file


def prepare_frames_cfr(
    temp_path: Path,
    frames: List[Image.Image],
    timestamps: List[TimestampInfo],
    video_duration: float,
    fps: float,
    progress,
    task
) -> int:
    """Prepare frames for CFR mode using image2 demuxer with symlinks. Returns total frame count."""
    frame_counter = 0

    for i, ts_info in enumerate(timestamps):
        # Calculate duration (time until next frame, or end of video)
        if i < len(timestamps) - 1:
            duration = timestamps[i + 1].timestamp - ts_info.timestamp
        else:
            duration = video_duration - ts_info.timestamp

        # Calculate how many times this frame should appear at target fps
        num_frames = max(1, round(duration * fps))

        # Get the image
        image_idx = ts_info.image_index
        frame = frames[image_idx]

        # Save unique frame once, then create symlinks for duplicates
        unique_frame_path = temp_path / f"unique_{i:06d}.png"
        frame.save(unique_frame_path, format='PNG')

        # Create references (first is the actual file, rest are symlinks)
        for j in range(num_frames):
            frame_path = temp_path / f"frame_{frame_counter:06d}.png"
            if j == 0:
                # First occurrence: rename the unique frame
                os.rename(unique_frame_path, frame_path)
                actual_frame_path = frame_path
            else:
                # Subsequent occurrences: create symlink
                os.symlink(actual_frame_path, frame_path)
            frame_counter += 1

        progress.update(task, advance=1)

    return frame_counter


def build_ffmpeg_pipeline(
    temp_path: Path,
    output_path: Path,
    mode: FrameMode,
    fps: float,
    concat_file: Path | None,
    video_codec: str,
    crf: int,
    preset: str,
    audio_file: str | None
):
    """Build and execute FFmpeg pipeline for video encoding."""
    # Build FFmpeg input based on mode
    if mode == "vfr":
        if concat_file is None:
            raise ValueError("concat_file is required for VFR mode")
        video_input = ffmpeg.input(str(concat_file), f='concat', safe=0)
    else:  # mode == "cfr"
        pattern = str(temp_path / "frame_%06d.png")
        video_input = ffmpeg.input(pattern, framerate=fps)

    # Build output with encoding options
    output_kwargs = {
        'vcodec': video_codec,
        'crf': crf,
        'preset': preset,
        'pix_fmt': 'yuv420p',  # For compatibility
    }

    # Add mode-specific options
    if mode == "vfr":
        output_kwargs['vsync'] = 'vfr'  # Variable frame rate - respects exact durations from concat

    # Create output with or without audio
    if audio_file:
        audio_path = Path(audio_file)
        if not audio_path.exists():
            raise FileNotFoundError(f"Audio file not found: {audio_file}")
        logger.info(f"Adding audio from: {audio_file}")
        audio_input = ffmpeg.input(str(audio_path))

        # Explicitly select video stream from concat input and audio stream from audio input
        output_kwargs['acodec'] = 'copy'  # Copy audio stream without re-encoding
        # Allow experimental codecs (like Opus in MP4) when copying audio
        output_kwargs['strict'] = 'experimental'

        # Map the streams
        stream = ffmpeg.output(video_input['v'], audio_input['a'], str(output_path), **output_kwargs)
    else:
        # Video only
        stream = ffmpeg.output(video_input, str(output_path), **output_kwargs)

    # Run FFmpeg
    try:
        ffmpeg.run(stream, overwrite_output=True, capture_stdout=True, capture_stderr=True)
    except ffmpeg.Error as e:
        logger.error(f"FFmpeg failed: {e.stderr.decode() if e.stderr else 'Unknown error'}")
        raise


def reassemble_video_from_folder(
    frames_folder: str,
    output_file: str,
    audio_file: str | None = None,
    video_codec: str = "libx264",
    crf: int = 23,
    preset: str = "medium",
    mode: FrameMode = "vfr",
    fps: float = 25.0
) -> None:
    """
    Reassemble video from extracted frames folder.

    Args:
        frames_folder: Path to folder containing frames and metadata.json
        output_file: Path to output video file
        audio_file: Optional path to audio file to include
        video_codec: FFmpeg video codec (default: libx264)
        crf: Constant Rate Factor for quality (0-51, lower is better, default: 23)
        preset: Encoding preset (ultrafast, superfast, veryfast, faster, fast, medium, slow, slower, veryslow)
        mode: Frame rate mode - "vfr" (variable, most accurate) or "cfr" (constant, better player compatibility)
        fps: Target framerate for CFR mode (default: 25.0)
    """
    folder_path = Path(frames_folder)
    output_path = Path(output_file)

    if not folder_path.exists() or not folder_path.is_dir():
        raise FileNotFoundError(f"Folder not found: {frames_folder}")

    logger.info(f"Loading metadata from: {frames_folder}")
    logger.info(f"Mode: {mode.upper()}" + (f" @ {fps} fps" if mode == "cfr" else ""))
    metadata = load_metadata(folder_path)

    # Load frames
    if metadata.format == "webp":
        webp_path = folder_path / "frames.webp"
        logger.info(f"Loading frames from: {webp_path}")
        frames = load_frames_from_webp(webp_path)
    else:
        logger.info("Loading frames from PNG files...")
        frames = load_frames_from_pngs(folder_path, metadata)

    logger.info(f"Loaded {len(frames)} unique images")
    logger.info("Building video timeline...")

    # Create a temporary directory for frame files
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        console = Console()

        # Prepare frames based on mode
        concat_file = None
        with Progress(
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
            TimeElapsedColumn(),
            console=console,
        ) as progress:
            task = progress.add_task("Preparing frames...", total=len(metadata.timestamps))

            if mode == "vfr":
                concat_file = prepare_frames_vfr(
                    temp_path,
                    frames,
                    metadata.timestamps,
                    metadata.video_info.duration,
                    progress,
                    task
                )
            else:  # mode == "cfr"
                prepare_frames_cfr(
                    temp_path,
                    frames,
                    metadata.timestamps,
                    metadata.video_info.duration,
                    fps,
                    progress,
                    task
                )

        logger.info("Encoding video with FFmpeg...")

        # Build and run FFmpeg pipeline
        build_ffmpeg_pipeline(
            temp_path,
            output_path,
            mode,
            fps,
            concat_file,
            video_codec,
            crf,
            preset,
            audio_file
        )

    logger.info(f"Video saved to: {output_path}")
    logger.info("Done!")


def main(
    frames_folder: str,
    output_file: str,
    audio_file: str | None = None,
    video_codec: str = "libx264",
    crf: int = 23,
    preset: str = "medium",
    mode: FrameMode = "vfr",
    fps: float = 25.0
) -> None:
    reassemble_video_from_folder(frames_folder, output_file, audio_file, video_codec, crf, preset, mode, fps)
