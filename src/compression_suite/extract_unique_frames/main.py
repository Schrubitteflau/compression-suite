#!/usr/bin/env python3
"""
Extract unique frames from frames recording using mpdecimate and perceptual hashing
"""

import logging
import re
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Dict, List

import ffmpeg
import imagehash
import numpy as np
from PIL import Image
from rich.progress import Progress, TimeElapsedColumn, BarColumn, TextColumn
from rich.console import Console

from compression_suite.models.metadata import Metadata, TimestampInfo, VideoInfo
from compression_suite.utils.video import get_video_info

HASH_THRESHOLD = 5

# Get logger for this module
logger = logging.getLogger(__name__)


@dataclass
class ExtractedFrameInfo:
    """Information about an extracted frame."""
    hash: imagehash.ImageHash | None = None
    timestamp: float | None = None  # Will be set by timestamp thread


def get_frame_info(frame_infos: List[ExtractedFrameInfo], idx: int) -> ExtractedFrameInfo:
    """Lazily get or create frame info at given index."""
    # Extend list if needed
    while len(frame_infos) <= idx:
        frame_infos.append(ExtractedFrameInfo())
    return frame_infos[idx]


def parse_timestamps(ffmpeg_pipe: IO[bytes], frame_infos: List[ExtractedFrameInfo]) -> None:
    """Parse timestamps from FFmpeg and assign them to frame_infos."""
    pattern: re.Pattern[str] = re.compile(r'pts_time:([0-9.]+)')
    frame_index = 0

    for lineb in iter(ffmpeg_pipe.readline, b''):
        line: str = lineb.decode('utf-8', errors='ignore')
        match: re.Match[str] | None = pattern.search(line)
        if match:
            timestamp = float(match.group(1))
            # Get or create frame info and set timestamp
            frame_info = get_frame_info(frame_infos, frame_index)
            frame_info.timestamp = timestamp
            frame_index += 1


def compute_hash(img: Image.Image) -> imagehash.ImageHash:
    """Compute perceptual hash for an image."""
    return imagehash.phash(img)


def is_different_from_previous(current_hash: imagehash.ImageHash, previous_hash: imagehash.ImageHash | None, threshold=HASH_THRESHOLD) -> bool:
    """Check if current frame is different from the previous frame."""
    if previous_hash is None:
        return True
    return abs(current_hash - previous_hash) > threshold


def extract_unique_frames_to_folder(
    video_file: str,
    output_folder: str,
    use_webp: bool = True,
    mpdecimate: bool = True
) -> None:
    """
    Extract unique frames from video to a folder.

    Args:
        video_file: Path to the input video file
        output_folder: Path to output folder
        use_webp: If True, save as multi-frame WebP, else save as individual PNGs
        mpdecimate: If True, use FFmpeg mpdecimate filter first
    """
    output_path = Path(output_folder)
    output_path.mkdir(parents=True, exist_ok=True)

    logger.info(f"Processing video: {video_file}")
    video_info = get_video_info(video_file)
    logger.info(f"Video: {video_info.width}x{video_info.height} @ {video_info.fps} fps")

    all_frames: List[ExtractedFrameInfo] = []
    unique_frames: List[ExtractedFrameInfo] = []
    unique_images: Dict[imagehash.ImageHash, Image.Image] = {}
    previous_frame_info: ExtractedFrameInfo | None = None

    try:
        # Build FFmpeg pipeline
        stream = ffmpeg.input(video_file)

        # Apply mpdecimate filter if requested
        if mpdecimate:
            logger.info("Applying mpdecimate filter...")
            # Hardcoded default mpdecimate settings - they work well for slides
            # hi=64*12 (768), lo=64*5 (320), frac=0.33
            stream = stream.filter('mpdecimate', hi=64*12, lo=64*5, frac=0.33)

        stream = stream.filter('showinfo')

        frame_extraction_process = (
            stream
            .output('pipe:', format='rawvideo', pix_fmt='rgb24', vsync='vfr')
            .run_async(pipe_stdout=True, pipe_stderr=True)
        )
    except Exception as e:
        logger.error(f"Failed to start FFmpeg: {e}")
        sys.exit(1)

    # Start timestamp parsing thread
    ts_thread = threading.Thread(target=parse_timestamps, args=(frame_extraction_process.stderr, all_frames))
    ts_thread.start()

    frame_size: int = video_info.width * video_info.height * 3
    frames_processed = 0

    logger.info("Extracting and deduplicating frames...")

    console = Console()

    # Create progress bar based on video duration with live counters
    with Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        # Main progress bar
        main_task = progress.add_task("Processing video...", total=video_info.duration)

        try:
            while True:
                raw_frame = frame_extraction_process.stdout.read(frame_size)
                if len(raw_frame) < frame_size:
                    progress.update(main_task, completed=video_info.duration)
                    break

                frame = np.frombuffer(raw_frame, dtype=np.uint8).reshape((video_info.height, video_info.width, 3))
                img: Image.Image = Image.fromarray(frame)

                # Compute hash
                current_hash = compute_hash(img)

                # Get or create frame info for this frame
                frame_info = get_frame_info(all_frames, frames_processed)
                frame_info.hash = current_hash
                frames_processed += 1

                # Update main progress bar: if timestamp is not available for this frame, use the previous one, it'll still be a good estimate
                if frame_info.timestamp is not None:
                    progress.update(main_task, completed=frame_info.timestamp)
                elif previous_frame_info is not None:
                    progress.update(main_task, completed=previous_frame_info.timestamp)

                previous_hash = None if previous_frame_info is None else previous_frame_info.hash

                # Check if different from previous frame
                if is_different_from_previous(current_hash, previous_hash):
                    # This is a frame change
                    unique_frames.append(frame_info)

                    # Store image if we haven't seen this hash before
                    if current_hash not in unique_images:
                        unique_images[current_hash] = img

                # Update progress description with live stats
                current_time = progress.tasks[0].completed if progress.tasks else 0
                progress.update(
                    main_task,
                    description=f"[cyan]{len(all_frames)} processed frames[/cyan] [yellow]{len(unique_frames)} distinct[/yellow] [green]{len(unique_images)} unique images[/green] [dim]({current_time:.1f}s / {video_info.duration:.1f}s)[/dim]"
                )

                previous_frame_info = frame_info

        except Exception as e:
            logger.error(f"Error during frame processing: {e}")
            frame_extraction_process.kill()
            sys.exit(1)

    frame_extraction_process.wait()
    ts_thread.join()

    logger.info(f"Extracted {len(all_frames)} frames from video")
    logger.info(f"Detected {len(unique_frames)} frame changes")
    logger.info(f"Found {len(unique_images)} unique images")

    # Build metadata with timestamps and hash references
    logger.info("Building metadata...")
    hash_to_index = {hash_val: i for i, hash_val in enumerate(unique_images.keys())}
    timestamps_data = [
        TimestampInfo(
            timestamp=frame_info.timestamp,
            hash=str(frame_info.hash),
            image_index=hash_to_index[frame_info.hash]
        )
        for frame_info in unique_frames
    ]

    # Save unique images
    if use_webp:
        # Save as single multi-frame WebP (only unique images, in order)
        logger.info("Saving as multi-frame WebP...")
        webp_path = output_path / "frames.webp"
        if unique_images and not webp_path.exists():
            images_list = list(unique_images.values())
            images_list[0].save(
                webp_path,
                format='WebP',
                save_all=True,
                append_images=images_list[1:] if len(images_list) > 1 else [],
                quality=95,
                method=6
            )
            logger.info(f"Saved {len(unique_images)} unique images to {webp_path}")
        elif webp_path.exists():
            logger.info(f"WebP file already exists: {webp_path}")
    else:
        # Save as individual PNGs using hash as filename (avoids duplicates)
        logger.info("Saving as individual PNGs...")
        saved_count = 0
        for hash_val, img in unique_images.items():
            png_path = output_path / f"{hash_val}.png"
            if not png_path.exists():
                img.save(png_path, format='PNG')
                saved_count += 1
        logger.info(f"Saved {saved_count} new images ({len(unique_images) - saved_count} already existed)")

    # Save metadata using Pydantic model
    metadata = Metadata(
        version="1.0",
        frame_changes_count=len(unique_frames),
        unique_images_count=len(unique_images),
        timestamps=timestamps_data,
        format="webp" if use_webp else "png",
        video_info=VideoInfo(
            width=video_info.width,
            height=video_info.height,
            fps=video_info.fps,
            duration=video_info.duration
        )
    )
    metadata_path = output_path / "metadata.json"
    with open(metadata_path, 'w') as f:
        f.write(metadata.model_dump_json(indent=2))

    logger.info(f"Saved metadata to {metadata_path}")
    logger.info("Done!")


def main(
    input_file: str,
    output_folder: str,
    use_webp: bool = True,
    mpdecimate: bool = True,
) -> None:
    extract_unique_frames_to_folder(input_file, output_folder, use_webp, mpdecimate)
