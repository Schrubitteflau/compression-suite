"""CLI tests for reassemble-video: the pinned ffmpeg runs for real, frames folders and audio fabricated at test time.

The frames folder is the one extract-unique-frames writes: `metadata.json`, then either `frames.webp` (one frame per
unique image) or one `<hash>.png` per unique image. Three slides, red, blue and green, shown for one second each.
"""

import json
import subprocess
from pathlib import Path

import pytest
from click.testing import Result
from PIL import Image
from typer.testing import CliRunner

from compression_suite.cli import app
from compression_suite.utils.pinned_tools import resolve_tool

runner = CliRunner()

SLIDES = {"red": (255, 0, 0), "blue": (0, 0, 255), "green": (0, 255, 0)}
SIZE = 64
DURATION = 3.0


def _tool(name: str) -> str:
    """The pinned ffmpeg or ffprobe, to fabricate and read the videos."""
    return str(resolve_tool(name))


def _make_frames_folder(
    folder: Path, frame_format: str, starts: tuple[float, ...] = (0.0, 1.0, 2.0), duration: float = DURATION
) -> Path:
    """Write a frames folder as extract-unique-frames would: the three slides, starting at `starts` (one second
    each by default), in a source video lasting `duration` seconds."""
    folder.mkdir()
    images = [Image.new("RGB", (SIZE, SIZE), color) for color in SLIDES.values()]
    timestamps = [
        {"timestamp": start, "hash": name, "image_index": index}
        for index, (name, start) in enumerate(zip(SLIDES, starts, strict=True))
    ]
    if frame_format == "webp":
        images[0].save(folder / "frames.webp", format="WEBP", save_all=True, append_images=images[1:], lossless=True)
    else:
        for name, image in zip(SLIDES, images, strict=True):
            image.save(folder / f"{name}.png")
    metadata = {
        "version": "1.1",
        "frame_changes_count": len(timestamps),
        "unique_images_count": len(images),
        "timestamps": timestamps,
        "format": frame_format,
        "video_info": {"width": SIZE, "height": SIZE, "fps": 25.0, "duration": duration},
    }
    (folder / "metadata.json").write_text(json.dumps(metadata))
    return folder


@pytest.fixture(params=["webp", "png"])
def frames_folder(request: pytest.FixtureRequest, tmp_path: Path) -> Path:
    return _make_frames_folder(tmp_path / f"frames-{request.param}", request.param)


@pytest.fixture(scope="module")
def opus_audio(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A 3-second sine wave in Opus, as the README's workflow extracts it from the original video."""
    path = tmp_path_factory.mktemp("audio") / "audio.opus"
    subprocess.run(
        [_tool("ffmpeg"), "-loglevel", "error", "-f", "lavfi", "-i", f"sine=frequency=440:duration={DURATION}"]
        + ["-c:a", "libopus", "-b:a", "32k", str(path)],
        check=True,
    )
    return path


def _invoke(*args: str) -> Result:
    return runner.invoke(app, ["reassemble-video", *args])


def _streams(video: Path) -> list[dict]:
    """The streams of a video, as ffprobe reads them."""
    output = subprocess.run(
        [_tool("ffprobe"), "-v", "error", "-show_streams", "-of", "json", str(video)],
        capture_output=True,
        check=True,
        text=True,
    ).stdout
    return json.loads(output)["streams"]


def _video_stream(video: Path) -> dict:
    return next(stream for stream in _streams(video) if stream["codec_type"] == "video")


def _timeline(video: Path) -> list[tuple[float, str]]:
    """Every frame of a video, decoded: its time in seconds and the slide it shows, by its centre pixel."""
    frames = subprocess.run(
        [_tool("ffprobe"), "-v", "error", "-select_streams", "v:0"]
        + ["-show_entries", "frame=best_effort_timestamp_time", "-of", "json", str(video)],
        capture_output=True,
        check=True,
        text=True,
    ).stdout
    times = [frame["best_effort_timestamp_time"] for frame in json.loads(frames)["frames"]]
    raw = subprocess.run(
        [_tool("ffmpeg"), "-loglevel", "error", "-i", str(video), "-map", "0:v:0", "-fps_mode", "passthrough"]
        + ["-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:"],
        capture_output=True,
        check=True,
    ).stdout
    frame_size = SIZE * SIZE * 3
    assert len(raw) == frame_size * len(times)
    centre = (SIZE // 2 * SIZE + SIZE // 2) * 3
    timeline = []
    for index, time in enumerate(times):
        pixel = raw[index * frame_size + centre : index * frame_size + centre + 3]
        slide = min(SLIDES, key=lambda name: sum(abs(a - b) for a, b in zip(pixel, SLIDES[name], strict=True)))
        assert sum(abs(a - b) for a, b in zip(pixel, SLIDES[slide], strict=True)) <= 48, (time, tuple(pixel))
        timeline.append((round(float(time), 3), slide))
    return timeline


def test_vfr_mode_shows_each_slide_at_its_time(frames_folder: Path, tmp_path: Path):
    output = tmp_path / "out.mp4"
    result = _invoke(str(frames_folder), str(output))
    assert result.exit_code == 0, result.output
    video = _video_stream(output)
    assert (video["codec_name"], video["width"], video["height"], video["pix_fmt"]) == ("h264", SIZE, SIZE, "yuv420p")
    assert [stream["codec_type"] for stream in _streams(output)] == ["video"]
    # The last slide may be shown twice: its own frame, then the one closing the concat list (README, known limits)
    assert _timeline(output)[:3] == [(0.0, "red"), (1.0, "blue"), (2.0, "green")]
    assert float(video["duration"]) == pytest.approx(DURATION, abs=0.1)


def test_vfr_mode_keeps_the_source_duration_with_unequal_slides(tmp_path: Path):
    # A long slide, then short ones: the system ffmpeg 4.2 made this video last 9.16 s instead of 6.5 s
    frames = _make_frames_folder(tmp_path / "frames", "png", starts=(0.0, 4.2, 4.9), duration=6.5)
    output = tmp_path / "out.mp4"
    result = _invoke(str(frames), str(output))
    assert result.exit_code == 0, result.output
    assert float(_video_stream(output)["duration"]) == pytest.approx(6.5, abs=0.1)
    timeline = _timeline(output)[:3]
    assert [slide for _, slide in timeline] == ["red", "blue", "green"]
    assert [time for time, _ in timeline] == pytest.approx([0.0, 4.2, 4.9], abs=0.05)


def test_cfr_mode_repeats_slides_at_the_requested_rate(frames_folder: Path, tmp_path: Path):
    output = tmp_path / "out.mp4"
    result = _invoke(str(frames_folder), str(output), "--mode", "cfr", "--fps", "10")
    assert result.exit_code == 0, result.output
    video = _video_stream(output)
    assert video["codec_name"] == "h264"
    assert video["avg_frame_rate"] == "10/1"
    assert _timeline(output) == [(i / 10, ["red", "blue", "green"][i // 10]) for i in range(30)]


@pytest.mark.parametrize("mode", ["vfr", "cfr"])
def test_audio_is_copied_into_the_video(frames_folder: Path, opus_audio: Path, tmp_path: Path, mode: str):
    output = tmp_path / "out.mp4"
    result = _invoke(str(frames_folder), str(output), "--audio", str(opus_audio), "--mode", mode)
    assert result.exit_code == 0, result.output
    audio = [stream for stream in _streams(output) if stream["codec_type"] == "audio"]
    assert [stream["codec_name"] for stream in audio] == ["opus"]
    assert float(audio[0]["duration"]) == pytest.approx(DURATION, abs=0.1)


def test_codec_option_chooses_the_video_encoder(tmp_path: Path):
    frames = _make_frames_folder(tmp_path / "frames", "png")
    output = tmp_path / "out.mp4"
    result = _invoke(str(frames), str(output), "--codec", "libx265", "--mode", "cfr", "--fps", "5")
    assert result.exit_code == 0, result.output
    assert _video_stream(output)["codec_name"] == "hevc"


def test_unknown_mode_is_refused_without_output(tmp_path: Path):
    frames = _make_frames_folder(tmp_path / "frames", "png")
    output = tmp_path / "out.mp4"
    result = _invoke(str(frames), str(output), "--mode", "bogus")
    assert result.exit_code == 1
    assert "Invalid mode" in result.output
    assert not output.exists()


def test_folder_without_metadata_is_refused(tmp_path: Path):
    output = tmp_path / "out.mp4"
    result = _invoke(str(tmp_path), str(output))
    assert result.exit_code == 1
    assert "metadata.json not found" in result.output
    assert not output.exists()


def test_missing_audio_file_is_refused(tmp_path: Path):
    frames = _make_frames_folder(tmp_path / "frames", "png")
    output = tmp_path / "out.mp4"
    result = _invoke(str(frames), str(output), "--audio", str(tmp_path / "absent.opus"))
    assert result.exit_code == 1
    assert "Audio file does not exist" in result.output
    assert not output.exists()


def test_ffmpeg_of_the_path_is_never_run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    frames = _make_frames_folder(tmp_path / "frames", "png")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("ffmpeg", "ffprobe"):  # would fail the command if it were run
        (bin_dir / name).write_text("#!/bin/sh\nexit 1\n")
        (bin_dir / name).chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    output = tmp_path / "out.mp4"
    result = _invoke(str(frames), str(output), "--mode", "cfr", "--fps", "5")
    assert result.exit_code == 0, result.output
    assert _video_stream(output)["codec_name"] == "h264"


def test_missing_pinned_ffmpeg_is_tool_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    frames = _make_frames_folder(tmp_path / "frames", "png")
    monkeypatch.setenv("COMPRESSION_SUITE_TOOLS_DIR", str(tmp_path / "tools"))
    output = tmp_path / "out.mp4"
    result = _invoke(str(frames), str(output))
    assert result.exit_code == 30, result.output
    assert "ffmpeg" in result.stderr
    assert "just install-tools" in result.stderr
    assert not output.exists()
