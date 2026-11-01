"""CLI tests for preview-portion: the pinned ffmpeg and ffprobe run for real, on videos fabricated at test time from
the pattern of the video samples (`five_seconds`: laid out like a Galaxy S23 portrait video, 5 s long)."""

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from click.testing import Result
from typer.testing import CliRunner

from compression_suite.cli import app
from compression_suite.utils.pinned_tools import resolve_tool
from compression_suite.video_samples import BLUE, GREEN, H264, HDR_PQ, RED

runner = CliRunner()

# 2022-08-09T10:11:12.654321Z, in nanoseconds
MTIME_NS = 1_660_039_872_654_321_000

WIDTH, HEIGHT, FPS = 128, 72, 30


def _ffmpeg(*args: str) -> None:
    """Run the pinned ffmpeg quietly, to fabricate a video."""
    subprocess.run([str(resolve_tool("ffmpeg")), "-v", "error", "-nostdin", "-y", *args], check=True)


def _pattern(seconds: float) -> str:
    """The samples' pattern: red left half, blue right half, green square top-left."""
    hex_colour = "0x{:02x}{:02x}{:02x}".format
    return (
        f"color=c={hex_colour(*RED)}:s={WIDTH}x{HEIGHT}:r={FPS}:d={seconds},"
        f"drawbox=x={WIDTH // 2}:y=0:w={WIDTH // 2}:h={HEIGHT}:c={hex_colour(*BLUE)}:t=fill,"
        f"drawbox=x=0:y=0:w=16:h=16:c={hex_colour(*GREEN)}:t=fill,"
        # a frame counter, so that consecutive frames differ
        "drawtext=text='%{n}':x=70:y=30:fontsize=20:fontcolor=white"
    )


def _make_phone_video(path: Path, seconds: float = 5, audio: bool = True, rotation: int = -90) -> Path:
    """HEVC in MP4 (tag `hvc1`) with a keyframe every second, AAC audio, stored as a landscape with a display
    matrix, as a S23 records a portrait."""
    unrotated = path.with_name(f"unrotated-{path.name}")
    _ffmpeg(
        *("-f", "lavfi", "-i", _pattern(seconds)),
        *(("-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}:sample_rate=48000") if audio else ()),
        *("-c:v", "libx265", "-x265-params", f"log-level=error:keyint={FPS}", "-preset", "ultrafast", "-crf", "30"),
        *("-pix_fmt", "yuv420p", "-tag:v", "hvc1"),
        *(("-c:a", "aac", "-b:a", "64k") if audio else ()),
        str(unrotated),
    )
    _ffmpeg("-display_rotation:v:0", str(rotation), "-i", str(unrotated), "-map", "0", "-c", "copy", str(path))
    unrotated.unlink()
    os.utime(path, ns=(MTIME_NS, MTIME_NS))
    return path


@pytest.fixture(scope="module")
def five_seconds(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _make_phone_video(tmp_path_factory.mktemp("videos") / "phone.mp4")


def _invoke(video: Path, output: Path, *args: str) -> Result:
    return runner.invoke(app, ["preview-portion", str(video), "-o", str(output), "--json", *args])


def _portion(video: Path, output: Path, *args: str) -> dict:
    """Run preview-portion --json and return its `result`."""
    result = _invoke(video, output, *args)
    assert result.exit_code == 0, result.output
    document = json.loads(result.stdout)
    assert (document["schema_version"], document["ok"], document["command"]) == (1, True, "preview-portion")
    return document["result"]


def _probe(video: Path) -> dict:
    output = subprocess.run(
        [str(resolve_tool("ffprobe")), "-v", "error", "-show_format", "-show_streams", "-count_frames"]
        + ["-of", "json", str(video)],
        capture_output=True,
        check=True,
        text=True,
    ).stdout
    return json.loads(output)


def _stream(video: Path, codec_type: str) -> dict:
    return next(stream for stream in _probe(video)["streams"] if stream["codec_type"] == codec_type)


def test_portion_of_3_seconds_is_rendered_in_h264_upright(five_seconds, tmp_path):
    output = tmp_path / "portion.mp4"
    result = _portion(five_seconds, output, "--preset", "default", "--start", "1")

    assert result["input"] == str(five_seconds)
    assert result["output"] == str(output)
    assert (result["preset"], result["crf"]) == ("default", 32)
    assert result["start_seconds"] == pytest.approx(1.0, abs=0.001)
    assert result["duration_seconds"] == pytest.approx(3.0, abs=0.001)
    video = _stream(output, "video")
    assert (video["codec_name"], video["pix_fmt"], video["width"], video["height"]) == ("h264", "yuv420p", 72, 128)
    assert video["nb_read_frames"] == "90"
    assert _stream(output, "audio")["codec_name"] == "opus"
    assert result["encoded"]["size_bytes"] > 0
    assert result["encoded"]["bitrate_kbps"] == pytest.approx(result["encoded"]["size_bytes"] * 8 / 3.0 / 1000, abs=0.1)


def test_last_portion_is_shorter(five_seconds, tmp_path):
    output = tmp_path / "portion.mp4"
    result = _portion(five_seconds, output, "--preset", "default", "--start", "4")
    assert result["start_seconds"] == pytest.approx(4.0, abs=0.001)
    assert result["duration_seconds"] == pytest.approx(1.0, abs=0.001)
    assert _stream(output, "video")["nb_read_frames"] == "30"


def _frame_times(video: Path) -> list[float]:
    """The presentation time of every video frame, in seconds, sorted."""
    output = subprocess.run(
        [str(resolve_tool("ffprobe")), "-v", "error", "-select_streams", "v:0", "-show_entries", "frame=pts_time"]
        + ["-of", "csv=p=0", str(video)],
        capture_output=True,
        check=True,
        text=True,
    ).stdout
    return sorted(float(line.strip(",")) for line in output.split())


@pytest.mark.parametrize("preset", ["default", "dance"], ids=["with-audio", "without-audio"])
def test_portion_lands_on_the_timeline_of_the_original(five_seconds, tmp_path, preset):
    """Between two frames, the portion starts at the next one: its frames, shifted by `start_seconds`, fall on the
    original's own frames."""
    output = tmp_path / "portion.mp4"
    result = _portion(five_seconds, output, "--preset", preset, "--start", "1.01")
    original = _frame_times(five_seconds)
    portion = [result["start_seconds"] + moment for moment in _frame_times(output)]
    assert portion[0] == pytest.approx(31 / FPS, abs=0.001)  # the first frame at or after 1.01 s
    for moment in portion:
        assert min(abs(moment - frame) for frame in original) < 0.001
    assert portion[-1] == pytest.approx(120 / FPS, abs=0.001)  # the last frame before 4.01 s
    assert result["start_seconds"] + result["duration_seconds"] == pytest.approx(121 / FPS, abs=0.001)


@pytest.mark.parametrize(
    ("preset", "crf"), [("default", None), ("screen", "40"), ("dance", None)], ids=["default", "crf-40", "dance"]
)
def test_portion_weighs_what_the_export_of_the_same_seconds_weighs(five_seconds, tmp_path, preset, crf):
    """The encoded portion is what the export would make of these seconds: compress-video, on a video made of them
    alone, gives a file of the same size, to its metadata (under 300 bytes): two CRF steps apart, the sizes differ by
    a thousand bytes."""
    three_seconds = tmp_path / "three_seconds.mp4"
    _ffmpeg("-i", str(five_seconds), "-map", "0", "-c", "copy", "-t", "3", str(three_seconds))
    crf_args = ("--crf", crf) if crf else ()
    export = tmp_path / "export.mp4"
    exported = runner.invoke(
        app, ["compress-video", str(three_seconds), "--preset", preset, *crf_args, "-o", str(export), "--json"]
    )
    assert exported.exit_code == 0, exported.output

    result = _portion(five_seconds, tmp_path / "portion.mp4", "--preset", preset, *crf_args, "--start", "0")

    assert (result["preset"], result["crf"]) == (preset, int(crf) if crf else {"default": 32, "dance": 45}[preset])
    assert result["encoded"]["size_bytes"] == pytest.approx(export.stat().st_size, abs=500)


def test_dance_portion_has_no_audio(five_seconds, tmp_path):
    output = tmp_path / "portion.mp4"
    _portion(five_seconds, output, "--preset", "dance", "--start", "0")
    assert [stream["codec_type"] for stream in _probe(output)["streams"]] == ["video"]


def _assert_error(result: Result, code: str, exit_code: int) -> dict:
    """Check a failure's envelope, error code and exit code, and return its `error`."""
    assert result.exit_code == exit_code, result.output
    document = json.loads(result.stdout)
    assert (document["ok"], document["command"], document["error"]["code"]) == (False, "preview-portion", code)
    return document["error"]


@pytest.mark.parametrize(
    "args",
    [
        ["--preset", "fast-and-tiny", "--start", "0"],
        ["--preset", "default", "--crf", "52", "--start", "0"],
        ["--preset", "default", "--crf", "-1", "--start", "0"],
        ["--preset", "default", "--crf", "32.5", "--start", "0"],
        ["--preset", "default", "--start", "-1"],
        ["--preset", "default", "--start", "nan"],
        ["--preset", "default", "--start", "zero"],
        ["--preset", "default", "--start", "0", "--duration", "0"],
        ["--preset", "default", "--start", "0", "--duration", "inf"],
        ["--start", "0"],
        ["--preset", "default"],
    ],
    ids=[
        "unknown-preset",
        "crf-above",
        "crf-below",
        "crf-not-int",
        "negative-start",
        "nan-start",
        "start-not-a-number",
        "zero-duration",
        "infinite-duration",
        "no-preset",
        "no-start",
    ],
)
def test_bad_options_are_usage_errors(five_seconds, tmp_path, args):
    output = tmp_path / "portion.mp4"
    _assert_error(_invoke(five_seconds, output, *args), "usage_error", 2)
    assert list(tmp_path.iterdir()) == []


def test_unknown_preset_lists_the_known_ones(five_seconds, tmp_path):
    error = _assert_error(
        _invoke(five_seconds, tmp_path / "p.mp4", "--preset", "nope", "--start", "0"), "usage_error", 2
    )
    assert error["details"] == {"preset": "nope", "known_presets": ["default", "screen", "dance"]}


def test_crf_out_of_bounds_gives_the_bounds(five_seconds, tmp_path):
    args = ("--preset", "default", "--crf", "52", "--start", "0")
    error = _assert_error(_invoke(five_seconds, tmp_path / "p.mp4", *args), "usage_error", 2)
    assert error["details"] == {"crf": 52, "crf_min": 0, "crf_max": 51}


@pytest.mark.parametrize("start", ["5", "5.0001", "60"])
def test_start_beyond_the_end_is_a_usage_error(five_seconds, tmp_path, start):
    error = _assert_error(
        _invoke(five_seconds, tmp_path / "portion.mp4", "--preset", "default", "--start", start), "usage_error", 2
    )
    assert "beyond the end of the video" in error["message"]
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("preset", ["default", "dance"])
def test_start_after_the_last_frame_is_a_usage_error(five_seconds, tmp_path, preset):
    """The last frame starts at 4.967 s: from 4.99 s, the portion would hold the end of the audio at most."""
    error = _assert_error(
        _invoke(five_seconds, tmp_path / "portion.mp4", "--preset", preset, "--start", "4.99"), "usage_error", 2
    )
    assert "after the last frame" in error["message"]
    assert list(tmp_path.iterdir()) == []


def test_missing_input_is_input_not_found(tmp_path):
    args = ("--preset", "default", "--start", "0")
    _assert_error(_invoke(tmp_path / "absent.mp4", tmp_path / "portion.mp4", *args), "input_not_found", 23)


@pytest.mark.parametrize("kind", ["text", "jpeg", "audio"])
def test_not_a_video_is_unsupported_format(tmp_path, kind):
    if kind == "text":
        path = tmp_path / "notes.mp4"
        path.write_text("Giraffes and other words, nothing like a video.\n" * 20)
    elif kind == "jpeg":
        path = tmp_path / "photo.mp4"
        _ffmpeg("-i", str(H264), "-frames:v", "1", "-f", "image2", "-c:v", "mjpeg", str(path))
    else:
        path = tmp_path / "audio.m4a"
        _ffmpeg("-i", str(H264), "-vn", "-c:a", "copy", str(path))
    output = tmp_path / "portion.mp4"
    _assert_error(_invoke(path, output, "--preset", "default", "--start", "0"), "unsupported_format", 21)
    assert not output.exists()


def test_truncated_video_is_input_corrupt(five_seconds, tmp_path):
    """Cut short, as by an interrupted sync: the index, which a phone writes last, is lost."""
    video = tmp_path / "truncated.mp4"
    data = five_seconds.read_bytes()
    video.write_bytes(data[: len(data) // 2])
    error = _assert_error(
        _invoke(video, tmp_path / "portion.mp4", "--preset", "default", "--start", "0"), "input_corrupt", 27
    )
    assert error["message"].startswith("The video can't be decoded")
    assert [p.name for p in tmp_path.iterdir()] == ["truncated.mp4"]


def _garble_slices(path: Path, first: int, count: int) -> None:
    """Garble the slice data of video packets, keeping each packet's length prefix, NAL header and the start of its
    slice header: the container stays sound, only the decoder sees damage."""
    packets = json.loads(
        subprocess.run(
            [str(resolve_tool("ffprobe")), "-v", "error", "-select_streams", "v:0"]
            + ["-show_entries", "packet=pos,size", "-of", "json", str(path)],
            capture_output=True,
            check=True,
            text=True,
        ).stdout
    )["packets"]
    data = bytearray(path.read_bytes())
    for packet in packets[first : first + count]:
        start, size = int(packet["pos"]), int(packet["size"])
        for i in range(start + 12, start + size):
            data[i] = (data[i] * 7 + 93) & 0xFF
    path.write_bytes(bytes(data))


def test_damage_within_the_portion_is_input_corrupt(five_seconds, tmp_path):
    """The first frame and the last seconds decode, so the video passes the probe; the portion meets the damage."""
    video = tmp_path / "damaged.mp4"
    shutil.copyfile(five_seconds, video)
    _garble_slices(video, 35, 3)  # 1.17 s to 1.23 s, between keyframes
    output = tmp_path / "portion.mp4"
    _portion(video, output, "--preset", "dance", "--start", "2")  # the probe passes: the damage is elsewhere
    output.unlink()
    error = _assert_error(_invoke(video, output, "--preset", "dance", "--start", "1"), "input_corrupt", 27)
    assert error["message"].startswith("The video can't be decoded")
    assert "truncated" not in error["message"]
    assert not output.exists()


def test_existing_output_is_kept_without_overwrite(five_seconds, tmp_path):
    output = tmp_path / "portion.mp4"
    output.write_bytes(b"previous")
    args = ("--preset", "default", "--start", "0")
    _assert_error(_invoke(five_seconds, output, *args), "output_exists", 24)
    assert output.read_bytes() == b"previous"

    _portion(five_seconds, output, *args, "--overwrite")
    assert _stream(output, "video")["codec_name"] == "h264"
    assert [p.name for p in tmp_path.iterdir()] == ["portion.mp4"]


@pytest.mark.parametrize("link", ["same", "hardlink", "symlink"])
def test_output_that_is_the_input_is_refused(tmp_path, link):
    video = _make_phone_video(tmp_path / "phone.mp4", seconds=1)
    before = video.read_bytes()
    output = tmp_path / "portion.mp4"
    if link == "same":
        output = video
    elif link == "hardlink":
        os.link(video, output)
    else:
        output.symlink_to(video)
    args = ("--preset", "default", "--start", "0", "--overwrite")
    _assert_error(_invoke(video, output, *args), "usage_error", 2)
    assert video.read_bytes() == before
    assert video.stat().st_mtime_ns == MTIME_NS


def test_original_is_never_modified(five_seconds, tmp_path):
    video = tmp_path / "phone.mp4"
    shutil.copyfile(five_seconds, video)
    os.utime(video, ns=(MTIME_NS, MTIME_NS))
    before = video.read_bytes()
    _portion(video, tmp_path / "portion.mp4", "--preset", "default", "--start", "2")
    assert video.read_bytes() == before
    assert video.stat().st_mtime_ns == MTIME_NS


def _displayed_pixels(video: Path) -> tuple[int, int, list[tuple[int, int, int]]]:
    """Width, height and RGB pixels of the first frame as a player shows it: ffmpeg applies any display matrix."""
    stream = _stream(video, "video")
    rotated = any(side.get("rotation", 0) % 180 for side in stream.get("side_data_list", []))
    width, height = (stream["height"], stream["width"]) if rotated else (stream["width"], stream["height"])
    raw = subprocess.run(
        [str(resolve_tool("ffmpeg")), "-v", "error", "-nostdin", "-i", str(video), "-map", "0:v:0"]
        + ["-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:"],
        capture_output=True,
        check=True,
    ).stdout
    assert len(raw) == width * height * 3
    return width, height, [(raw[i], raw[i + 1], raw[i + 2]) for i in range(0, len(raw), 3)]


def _assert_colour(pixel: tuple[int, int, int], expected: tuple[int, int, int]) -> None:
    assert sum(abs(a - b) for a, b in zip(pixel, expected, strict=True)) <= 60, (pixel, expected)


@pytest.mark.parametrize("rotation", [-90, 90, 180, 0])
def test_portion_is_displayed_as_the_original(tmp_path, rotation):
    """Shown as the original is shown: the portion carries no display matrix, its pixels are turned instead."""
    video = _make_phone_video(tmp_path / "phone.mp4", seconds=1, rotation=rotation)
    output = tmp_path / "portion.mp4"
    _portion(video, output, "--preset", "default", "--start", "0")
    assert "side_data_list" not in _stream(output, "video")
    original_width, original_height, original = _displayed_pixels(video)
    width, height, pixels = _displayed_pixels(output)
    assert (width, height) == (original_width, original_height)
    for x, y in [(4, 4), (width - 5, 4), (4, height - 5), (width - 5, height - 5)]:
        _assert_colour(pixels[y * width + x], original[y * width + x])


def test_hdr_is_tone_mapped_to_sdr(tmp_path):
    output = tmp_path / "portion.mp4"
    _portion(HDR_PQ, output, "--preset", "default", "--start", "0")
    shown = _stream(output, "video")
    assert (shown["codec_name"], shown["pix_fmt"]) == ("h264", "yuv420p")
    assert (shown["color_transfer"], shown["color_primaries"], shown["color_space"]) == ("bt709",) * 3
    _, _, pixels = _displayed_pixels(output)
    assert max(sum(pixel) for pixel in pixels) > 150  # tone-mapped, not black


@pytest.fixture(scope="module")
def long_hevc(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """An HEVC detailed enough (720p, 20 s) for a portion of 10 s to take a few seconds, to interrupt it halfway."""
    path = tmp_path_factory.mktemp("long") / "long.mp4"
    _ffmpeg(
        *("-f", "lavfi", "-i", "testsrc2=s=1280x720:r=30:d=20", "-c:v", "libx265", "-preset", "ultrafast"),
        *("-x265-params", "log-level=error", str(path)),
    )
    return path


# What reading /proc raises for a process gone meanwhile: FileNotFoundError when it is gone before the open,
# ProcessLookupError when it is reaped between the open and the read (the ffprobe or the first ffmpeg ending while
# the next stage is looked for)
GONE = (FileNotFoundError, ProcessLookupError)


def _children(pid: int) -> list[int]:
    """The child processes of a process, from /proc."""
    children = []
    for task in Path(f"/proc/{pid}/task").iterdir():
        try:
            children += [int(child) for child in (task / "children").read_text().split()]
        except GONE:  # a thread that just ended
            continue
    return children


def _wait_for_encoder(pid: int, encoder: str) -> int:
    """Wait until a process runs an ffmpeg with this encoder, and return its pid."""
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        for child in _children(pid):
            try:
                if encoder in Path(f"/proc/{child}/cmdline").read_text():
                    return child
            except GONE:
                continue
        time.sleep(0.01)
    raise AssertionError(f"no ffmpeg running {encoder}")


def _is_running(pid: int) -> bool:
    try:
        return Path(f"/proc/{pid}/stat").read_text().split(")")[-1].split()[0] != "Z"
    except GONE:
        return False


@pytest.mark.parametrize(
    ("encoder", "sig"),
    [
        *(("libx265", sig) for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGKILL)),
        *(("libx264", sig) for sig in (signal.SIGTERM, signal.SIGKILL)),
    ],
    ids=lambda value: {"libx265": "encoding", "libx264": "rendering"}.get(value, getattr(value, "name", "")),
)
def test_interrupted_portion_leaves_nothing(tmp_path, long_hevc, sig, encoder):
    """Killed while encoding the portion or rendering it for display: no partial output, no temporary file, in the
    output's folder or the temporary folder, and no ffmpeg left running."""
    output_dir, temporary = tmp_path / "out", tmp_path / "tmp"
    output_dir.mkdir()
    temporary.mkdir()
    output = output_dir / "portion.mp4"
    process = subprocess.Popen(
        [sys.executable, "-m", "compression_suite", "preview-portion", str(long_hevc), "--preset", "default"]
        + ["--start", "5", "--duration", "10", "-o", str(output), "--json"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        env={**os.environ, "TMPDIR": str(temporary)},
    )
    ffmpeg = _wait_for_encoder(process.pid, encoder)
    time.sleep(0.1)  # let it write (the rendering, the shorter stage, lasts about 0.6 s)
    assert _is_running(ffmpeg)  # the signal lands in the targeted stage, not in the next one or after the end
    process.send_signal(sig)
    stdout, _ = process.communicate(timeout=60)

    if sig is signal.SIGKILL:
        assert process.returncode == -signal.SIGKILL
    else:
        assert process.returncode == 130
        assert json.loads(stdout)["error"]["code"] == "interrupted"
    deadline = time.monotonic() + 1
    while _is_running(ffmpeg) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not _is_running(ffmpeg)  # ffmpeg doesn't keep encoding on its own
    assert list(output_dir.iterdir()) == []
    assert list(temporary.iterdir()) == []
