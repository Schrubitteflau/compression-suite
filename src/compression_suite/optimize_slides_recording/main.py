#!/usr/bin/env python3
"""
In-memory slide reconstruction with perfect durations using FFmpeg filter_complex
"""

import logging
import math
import re
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import IO, List, Optional, Tuple

import imagehash
import numpy as np
from PIL import Image
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, TimeElapsedColumn

from compression_suite.utils.video import VideoInfo, get_video_info

# ----------------------------
# Constants
# ----------------------------
HASH_THRESHOLD = 5

# Get logger for this module
logger = logging.getLogger(__name__)


# Parse timestamps from FFmpeg pipe
def parse_timestamps(ffmpeg_pipe: IO[bytes], ts_list: List[float]) -> None:
    pattern: re.Pattern[str] = re.compile(r'pts_time:([0-9.]+)')
    for lineb in iter(ffmpeg_pipe.readline, b''):
        line: str = lineb.decode('utf-8', errors='ignore')
        match: re.Match[str] | None = pattern.search(line)
        if match:
            ts_list.append(float(match.group(1)))

def deduplicate_frame(img: Image.Image, unique_hashes: List[imagehash.ImageHash], threshold=HASH_THRESHOLD):
    h: imagehash.ImageHash = imagehash.phash(img)
    if all(abs(h - existing) > threshold for existing in unique_hashes):
        unique_hashes.append(h)
        return True
    return False

# ----------------------------
# Extract frames + timestamps
# ----------------------------
def extract_unique_frames(video_file: str, video_info: VideoInfo) -> Tuple[List[Image.Image], List[float]]:
    """
    Extract unique frames from video using perceptual hashing.

    Args:
        video_file: Path to the input video file
        video_info: Video information (width, height, fps, etc.)

    Returns:
        Tuple of (unique_frames, timestamps)
    """
    ts_list: list[float] = []
    unique_frames: list[Image.Image] = []
    unique_hashes: list[imagehash.ImageHash] = []
    timestamps: list[float] = []

    ffmpeg_cmd: list[str] = ["ffmpeg", "-i", video_file, "-f", "rawvideo", "-pix_fmt", "rgb24", "-vf", "showinfo", "-"]

    try:
        proc: subprocess.Popen[bytes] = subprocess.Popen(ffmpeg_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=10**8)
    except Exception as e:
        logger.error(f"Failed to start FFmpeg: {e}")
        sys.exit(1)

    ts_thread = threading.Thread(target=parse_timestamps, args=(proc.stderr, ts_list))
    ts_thread.start()

    frame_size: int = video_info.width * video_info.height * 3
    frame_index = 0

    # Determine total frames for progress bar
    total_frames: int = video_info.frame_count

    logger.info("Starting frame extraction and deduplication...")

    # Configure progress bar columns based on whether we know the total
    progress_columns = [
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
    ]

    if total_frames:
        progress_columns.extend([
            BarColumn(),
            TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        ])

    progress_columns.extend([
        TextColumn("•"),
        TextColumn("[cyan]{task.fields[frames_processed]}[/cyan] frames processed"),
        TextColumn("•"),
        TextColumn("[green]{task.fields[unique_slides]}[/green] unique slides"),
        TimeElapsedColumn(),
    ])

    with Progress(*progress_columns, transient=True) as progress:
        task = progress.add_task(
            "Extracting frames",
            total=total_frames,
            frames_processed=0,
            unique_slides=0
        )

        try:
            while True:
                raw_frame = proc.stdout.read(frame_size)
                if len(raw_frame) < frame_size:
                    break

                frame_index += 1
                frame = np.frombuffer(raw_frame, dtype=np.uint8).reshape((video_info.height, video_info.width, 3))
                img: Image.Image = Image.fromarray(frame)

                if deduplicate_frame(img, unique_hashes):
                    while frame_index-1 >= len(ts_list):
                        time.sleep(0.001)
                    ts: float = ts_list[frame_index-1]
                    timestamps.append(ts)
                    unique_frames.append(img)

                # Update progress
                progress.update(
                    task,
                    advance=1,
                    frames_processed=frame_index,
                    unique_slides=len(unique_frames)
                )
        except Exception as e:
            logger.error(f"Error during frame processing: {e}")
            proc.kill()
            sys.exit(1)

    proc.wait()
    ts_thread.join()

    logger.info(f"Detected {len(unique_frames)} unique slides from {frame_index} frames.")
    return unique_frames, timestamps

def compute_durations_and_frames(timestamps: List[float], fps: float) -> Tuple[List[float], List[int]]:
    """
    Calculate duration and frame count for each slide.

    Args:
        timestamps: List of timestamps when slides changed
        fps: Frames per second of the video

    Returns:
        Tuple of (durations, frames_per_slide)
    """
    durations: list[float] = []
    frames_per_slide: list[int] = []
    for i in range(len(timestamps)-1):
        dur: float = timestamps[i+1] - timestamps[i]
        durations.append(dur)
        frames_per_slide.append(max(1, math.ceil(dur * fps)))
    durations.append(2.0)  # fallback last slide
    frames_per_slide.append(max(1, math.ceil(2.0 * fps)))

    logger.info(f"Frame duration error <= {1000.0/fps:.1f} ms")
    return durations, frames_per_slide

# Rebuild video using filter_complex loops for perfect frame timing and sync with audio
def rebuild_video_perfect(
    unique_frames: List[Image.Image],
    frames_per_slide: List[int],
    video_info: VideoInfo,
    input_video: str,
    output_video: str,
    audio_codec: Optional[str] = None,
    audio_bitrate: Optional[str] = None,
) -> None:
    """
    Rebuild video with deduplicated slides and original audio.

    Args:
        unique_frames: List of unique slide images
        frames_per_slide: Number of frames for each slide
        video_info: Video information (width, height, fps, etc.)
        input_video: Path to input video file
        output_video: Path to output video file
        audio_codec: Audio codec to use (None to copy)
        audio_bitrate: Audio bitrate (e.g., "128k")
    """
    num_slides: int = len(unique_frames)
    filter_parts:list[str] = []

    # Split input into N copies (one for each slide)
    split_outputs: str = "".join(f"[tmp{i}]" for i in range(num_slides))
    filter_parts.append(f"[0:v]split={num_slides}{split_outputs};")

    # For each slide, select the corresponding frame, loop it, and set PTS
    for i in range(num_slides):
        # Use select to extract frame i from the copied stream
        # Note: select uses eq(n\,X) where X is the frame number (0-indexed)
        filter_parts.append(
            f"[tmp{i}]select='eq(n\\,{i})',"
            f"loop=loop={frames_per_slide[i]-1}:size=1:start=0,"
            f"setpts=N/({video_info.fps})/TB[s{i}out];"
        )

    # Concat all the looped slides
    concat_inputs: str = "".join(f"[s{i}out]" for i in range(num_slides))
    filter_parts.append(f"{concat_inputs}concat=n={num_slides}:v=1:a=0[vout]")

    filter_complex:str = "".join(filter_parts)
    logger.debug(f"Filter complex: {filter_complex}")

    ffmpeg_cmd: List[str] = [
        "ffmpeg", "-y",
        "-f", "rawvideo", "-pix_fmt", "rgb24",
        "-s", f"{video_info.width}x{video_info.height}", "-r", str(video_info.fps),
        "-i", "-",  # stdin
        "-i", input_video,
        "-filter_complex", filter_complex,
        "-map", "[vout]",
        "-map", "1:a:0",
        "-pix_fmt", "yuv420p",
        "-c:v", "libx264"
    ]

    logger.debug(f"FFmpeg command: {' '.join(ffmpeg_cmd)}")

    if audio_codec:
        ffmpeg_cmd += ["-c:a", audio_codec]
        if audio_bitrate:
            ffmpeg_cmd += ["-b:a", audio_bitrate]
    else:
        ffmpeg_cmd += ["-c:a", "copy"]

    ffmpeg_cmd.append(output_video)

    try:
        proc = subprocess.Popen(ffmpeg_cmd, stdin=subprocess.PIPE)
        logger.info("Rebuilding video with perfect slide durations and audio...")

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
            TextColumn("•"),
            TextColumn("[cyan]{task.completed}/{task.total}[/cyan] slides"),
            TimeElapsedColumn(),
            transient=True,
        ) as progress:
            task = progress.add_task("Writing slides", total=len(unique_frames))

            for img in unique_frames:
                proc.stdin.write(img.tobytes())
                progress.advance(task)

        proc.stdin.close()
        proc.wait()
    except Exception as e:
        logger.error(f"Error during video rebuild: {e}")
        sys.exit(1)

    logger.info(f"Rebuilt video saved as {output_video}")


def main(
    input_file: str,
    output_file: str,
    audio_codec: Optional[str] = None,
    audio_bitrate: Optional[str] = None,
) -> None:
    """
    Optimize a slides recording by deduplicating similar frames.

    Args:
        input_file: Path to input video file
        output_file: Path to output video file
        audio_codec: Audio codec to use (None to copy from source)
        audio_bitrate: Audio bitrate (e.g., "128k")
    """
    logger.info(f"Processing video: {input_file}")
    logger.info(f"Output will be saved to: {output_file}")

    video_info = get_video_info(input_file)
    unique_frames, timestamps = extract_unique_frames(input_file, video_info)
    durations, frames_per_slide = compute_durations_and_frames(timestamps, video_info.fps)
    rebuild_video_perfect(
        unique_frames,
        frames_per_slide,
        video_info,
        input_file,
        output_file,
        audio_codec,
        audio_bitrate,
    )
