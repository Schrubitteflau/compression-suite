#!/usr/bin/env python3
"""
Reassemble video from extracted unique frames
"""

import json
import logging
import tempfile
from pathlib import Path
from typing import Any, Dict, List

import ffmpeg
from PIL import Image
from rich.console import Console
from rich.progress import Progress, TimeElapsedColumn, BarColumn, TextColumn

logger = logging.getLogger(__name__)


def load_metadata(folder_path: Path) -> Dict[str, Any]:
    """Load metadata.json from the folder."""
    metadata_path = folder_path / "metadata.json"
    if not metadata_path.exists():
        raise FileNotFoundError(f"metadata.json not found in {folder_path}")

    with open(metadata_path, 'r') as f:
        return json.load(f)


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


def load_frames_from_pngs(folder_path: Path, metadata: Dict[str, Any]) -> List[Image.Image]:
    """Load frames from individual PNG files based on metadata."""
    frames = []

    # Get unique image hashes in order
    timestamps = metadata["timestamps"]
    seen_indices = set()
    image_hashes = []

    for ts in timestamps:
        idx = ts["image_index"]
        if idx not in seen_indices:
            seen_indices.add(idx)
            image_hashes.append(ts["hash"])

    # Load images
    for hash_val in image_hashes:
        png_path = folder_path / f"{hash_val}.png"
        if not png_path.exists():
            raise FileNotFoundError(f"PNG file not found: {png_path}")
        frames.append(Image.open(png_path))

    return frames


def reassemble_video_from_folder(
    frames_folder: str,
    output_file: str,
    audio_file: str | None = None,
    video_codec: str = "libx264",
    crf: int = 23,
    preset: str = "medium"
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
    """
    folder_path = Path(frames_folder)
    output_path = Path(output_file)

    if not folder_path.exists() or not folder_path.is_dir():
        raise FileNotFoundError(f"Folder not found: {frames_folder}")

    logger.info(f"Loading metadata from: {frames_folder}")
    metadata = load_metadata(folder_path)

    # Load frames
    if metadata["format"] == "webp":
        webp_path = folder_path / "frames.webp"
        logger.info(f"Loading frames from: {webp_path}")
        frames = load_frames_from_webp(webp_path)
    else:
        logger.info("Loading frames from PNG files...")
        frames = load_frames_from_pngs(folder_path, metadata)

    logger.info(f"Loaded {len(frames)} unique images")

    # Build frame durations from timestamps
    timestamps = metadata["timestamps"]
    video_info = metadata["video_info"]

    logger.info("Building video timeline...")

    # Create a temporary directory for frame files and concat file
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        console = Console()

        # Create concat demuxer file
        concat_file = temp_path / "concat.txt"

        with Progress(
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
            TimeElapsedColumn(),
            console=console,
        ) as progress:
            task = progress.add_task("Preparing frames...", total=len(timestamps))

            with open(concat_file, 'w') as f:
                for i, ts_info in enumerate(timestamps):
                    # Calculate duration (time until next frame, or end of video)
                    if i < len(timestamps) - 1:
                        duration = timestamps[i + 1]["timestamp"] - ts_info["timestamp"]
                    else:
                        duration = video_info["duration"] - ts_info["timestamp"]

                    # Get the image and save it
                    image_idx = ts_info["image_index"]
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

        logger.info("Encoding video with FFmpeg...")

        # Build FFmpeg pipeline using concat demuxer with vsync vfr for exact durations
        video_input = ffmpeg.input(str(concat_file), f='concat', safe=0)

        # Build output with encoding options
        output_kwargs = {
            'vcodec': video_codec,
            'crf': crf,
            'preset': preset,
            'pix_fmt': 'yuv420p',  # For compatibility
            'vsync': 'vfr',  # Variable frame rate - respects exact durations from concat
        }

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
            # Note: Don't use -shortest because concat demuxer reports Duration: N/A

            # Map the streams: use ffmpeg directly with proper mapping
            stream = (
                ffmpeg
                .output(video_input['v'], audio_input['a'], str(output_path), **output_kwargs)
            )
        else:
            # Video only
            stream = ffmpeg.output(video_input, str(output_path), **output_kwargs)

        # Run FFmpeg
        try:
            ffmpeg.run(stream, overwrite_output=True, capture_stdout=True, capture_stderr=True)
        except ffmpeg.Error as e:
            logger.error(f"FFmpeg failed: {e.stderr.decode() if e.stderr else 'Unknown error'}")
            raise

    logger.info(f"Video saved to: {output_path}")
    logger.info("Done!")


def main(
    frames_folder: str,
    output_file: str,
    audio_file: str | None = None,
    video_codec: str = "libx264",
    crf: int = 23,
    preset: str = "medium"
) -> None:
    reassemble_video_from_folder(frames_folder, output_file, audio_file, video_codec, crf, preset)
