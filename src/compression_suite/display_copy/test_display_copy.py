"""CLI tests for display-copy: the pinned ffmpeg and ffprobe run for real, on the fabricated video samples."""

import json
import os
import resource
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from unittest import mock

import pytest
from click.testing import Result
from typer.testing import CliRunner

from compression_suite.cli import app
from compression_suite.display_copy import main as display_copy_main
from compression_suite.utils.pinned_tools import resolve_tool
from compression_suite.video_samples import BLUE, GREEN, H264, HDR_PQ, HEVC_ROTATED, RED

runner = CliRunner()

# 2022-08-09T10:11:12.654321Z, in nanoseconds
MTIME_NS = 1_660_039_872_654_321_000
MTIME_ISO = "2022-08-09T10:11:12.654321+00:00"


def _copy(src: Path, tmp_path: Path, name: str | None = None) -> Path:
    """Copy a sample with a known mtime."""
    path = tmp_path / (name or src.name)
    shutil.copyfile(src, path)
    os.utime(path, ns=(MTIME_NS, MTIME_NS))
    return path


def _ffmpeg(*args: str) -> None:
    """Run the pinned ffmpeg quietly, to derive a variant of a sample."""
    subprocess.run([str(resolve_tool("ffmpeg")), "-v", "error", "-nostdin", "-y", *args], check=True)


def _invoke(path: Path, output: Path, *args: str) -> Result:
    return runner.invoke(app, ["display-copy", str(path), "-o", str(output), "--json", *args])


def _display_copy(path: Path, output: Path, *args: str) -> dict:
    """Run display-copy --json and return its `result`."""
    result = _invoke(path, output, *args)
    assert result.exit_code == 0, result.output
    document = json.loads(result.stdout)
    assert document["schema_version"] == 2
    assert document["ok"] is True
    assert document["command"] == "display-copy"
    return document["result"]


def _assert_error(result: Result, code: str, exit_code: int) -> dict:
    """Check a failure's envelope, error code and exit code, and return its `error`."""
    assert result.exit_code == exit_code, result.output
    document = json.loads(result.stdout)
    assert document["ok"] is False
    assert document["command"] == "display-copy"
    assert document["error"]["code"] == code
    return document["error"]


def _streams(video: Path) -> list[dict]:
    """The streams of a video, as the pinned ffprobe reads them."""
    output = subprocess.run(
        [str(resolve_tool("ffprobe")), "-v", "error", "-show_streams", "-count_frames", "-of", "json", str(video)],
        capture_output=True,
        check=True,
        text=True,
    ).stdout
    return json.loads(output)["streams"]


def _stream(video: Path, codec_type: str) -> dict:
    return next(stream for stream in _streams(video) if stream["codec_type"] == codec_type)


def test_playable_h264_needs_no_copy(tmp_path):
    video = _copy(H264, tmp_path)
    output = tmp_path / "out" / "copy.mp4"
    result = _display_copy(video, output)
    assert isinstance(result.pop("elapsed_seconds"), float)
    assert result == {
        "input": {"path": str(video), "format": "mp4", "size_bytes": H264.stat().st_size, "mtime": MTIME_ISO},
        "output": None,
        "duration_seconds": pytest.approx(1.0, abs=0.05),
        "copy_needed": False,
        "copy_reasons": [],
        "width": 128,
        "height": 72,
        "fps": 30.0,
        "video_codec": "h264",
        "audio_codec": "aac",
    }
    assert not output.parent.exists()  # nothing written, not even the folder


def _displayed_pixels(video: Path) -> tuple[int, int, list[tuple[int, int, int]]]:
    """Width, height and RGB pixels of the middle frame as a player shows it: ffmpeg applies any display matrix."""
    stream = _stream(video, "video")
    rotated = any(side.get("rotation", 0) % 180 for side in stream.get("side_data_list", []))
    width, height = (stream["height"], stream["width"]) if rotated else (stream["width"], stream["height"])
    raw = subprocess.run(
        [str(resolve_tool("ffmpeg")), "-v", "error", "-nostdin", "-ss", "0.5", "-i", str(video), "-map", "0:v:0"]
        + ["-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:"],
        capture_output=True,
        check=True,
    ).stdout
    assert len(raw) == width * height * 3
    pixels = [(raw[i], raw[i + 1], raw[i + 2]) for i in range(0, len(raw), 3)]
    return width, height, pixels


def _assert_colour(pixel: tuple[int, int, int], expected: tuple[int, int, int]) -> None:
    assert sum(abs(a - b) for a, b in zip(pixel, expected, strict=True)) <= 60, (pixel, expected)


def test_hevc_is_copied_to_h264_upright_with_audio(tmp_path):
    video = _copy(HEVC_ROTATED, tmp_path)
    before = video.read_bytes()
    output = tmp_path / "out" / "copy.mp4"

    result = _display_copy(video, output)

    assert isinstance(result.pop("elapsed_seconds"), float)
    written = output.stat()
    assert result["output"]["mtime"].endswith("+00:00")  # the copy's own, in UTC
    assert result == {
        "input": {"path": str(video), "format": "mp4", "size_bytes": len(before), "mtime": MTIME_ISO},
        "output": {"path": str(output), "format": "mp4", "size_bytes": written.st_size, "mtime": mock.ANY},
        "duration_seconds": pytest.approx(1.0, abs=0.05),
        "copy_needed": True,
        "copy_reasons": ["video codec hevc"],
        "width": 72,  # displayed as a portrait
        "height": 128,
        "fps": 30.0,
        "video_codec": "hevc",
        "audio_codec": "aac",
    }
    copied = _stream(output, "video")
    assert (copied["codec_name"], copied["pix_fmt"], copied["avg_frame_rate"]) == ("h264", "yuv420p", "30/1")
    assert copied["nb_read_frames"] == _stream(video, "video")["nb_read_frames"] == "30"
    assert _stream(output, "audio")["codec_name"] == "aac"

    # Shown as the original is: a portrait, red top half, blue bottom half, green square top-right
    width, height, pixels = _displayed_pixels(output)
    assert (width, height) == (72, 128)
    _assert_colour(pixels[32 * width + 36], RED)
    _assert_colour(pixels[96 * width + 36], BLUE)
    _assert_colour(pixels[8 * width + 64], GREEN)
    assert _displayed_pixels(video)[:2] == (72, 128)

    assert video.read_bytes() == before
    assert video.stat().st_mtime_ns == MTIME_NS
    assert sorted(p.name for p in output.parent.iterdir()) == ["copy.mp4"]


@pytest.mark.parametrize(
    ("name", "ffmpeg_args"),
    [
        ("mov.mov", ("-c", "copy", "-f", "mov")),  # brand `qt  `
        ("silent.mp4", ("-an", "-c", "copy")),
        ("mp3.mp4", ("-c:v", "copy", "-c:a", "libmp3lame")),
        ("opus.mp4", ("-c:v", "copy", "-c:a", "libopus")),
    ],
)
def test_other_playable_videos_need_no_copy(tmp_path, name, ffmpeg_args):
    video = tmp_path / name
    _ffmpeg("-i", str(H264), *ffmpeg_args, str(video))
    output = tmp_path / "copy.mp4"
    result = _display_copy(video, output)
    assert (result["copy_needed"], result["copy_reasons"], result["video_codec"]) == (False, [], "h264")
    assert not output.exists()


def test_rotated_h264_needs_no_copy_and_shows_its_displayed_size(tmp_path):
    video = tmp_path / "rotated.mp4"
    _ffmpeg("-display_rotation:v:0", "-90", "-i", str(H264), "-c", "copy", str(video))
    result = _display_copy(video, tmp_path / "copy.mp4")
    assert (result["copy_needed"], result["width"], result["height"]) == (False, 72, 128)


def test_h264_in_matroska_is_remuxed_without_reencoding(tmp_path):
    video = tmp_path / "video.mkv"
    _ffmpeg("-i", str(H264), "-c", "copy", str(video))
    output = tmp_path / "copy.mp4"
    result = _display_copy(video, output)
    assert (result["copy_needed"], result["copy_reasons"]) == (True, ["container matroska,webm"])
    original_packets = subprocess.run(
        [str(resolve_tool("ffprobe")), "-v", "error", "-select_streams", "v:0", "-show_entries", "packet=size"]
        + ["-of", "csv=p=0", str(H264)],
        capture_output=True,
        check=True,
        text=True,
    ).stdout
    copied_packets = subprocess.run(
        [str(resolve_tool("ffprobe")), "-v", "error", "-select_streams", "v:0", "-show_entries", "packet=size"]
        + ["-of", "csv=p=0", str(output)],
        capture_output=True,
        check=True,
        text=True,
    ).stdout
    assert copied_packets == original_packets  # the H.264 stream is copied as is
    assert _stream(output, "audio")["codec_name"] == "aac"


def test_unplayable_audio_is_reencoded_to_aac(tmp_path):
    video = tmp_path / "pcm.mov"
    _ffmpeg("-i", str(H264), "-c:v", "copy", "-c:a", "pcm_s16le", str(video))
    output = tmp_path / "copy.mp4"
    result = _display_copy(video, output)
    assert (result["copy_needed"], result["copy_reasons"], result["audio_codec"]) == (
        True,
        ["audio codec pcm_s16le"],
        "pcm_s16le",
    )
    assert _stream(output, "video")["codec_name"] == "h264"
    assert _stream(output, "audio")["codec_name"] == "aac"


def test_10_bit_h264_is_copied_to_8_bits(tmp_path):
    video = tmp_path / "10bit.mp4"
    _ffmpeg(
        "-i",
        str(H264),
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-pix_fmt",
        "yuv420p10le",
        "-c:a",
        "copy",
        str(video),
    )
    output = tmp_path / "copy.mp4"
    result = _display_copy(video, output)
    assert (result["copy_needed"], result["copy_reasons"]) == (True, ["pixel format yuv420p10le"])
    copied = _stream(output, "video")
    assert (copied["codec_name"], copied["pix_fmt"], copied["width"], copied["height"]) == ("h264", "yuv420p", 128, 72)


def test_missing_input_is_input_not_found(tmp_path):
    _assert_error(_invoke(tmp_path / "absent.mp4", tmp_path / "copy.mp4"), "input_not_found", 23)


def _make_not_a_video(tmp_path: Path, kind: str) -> Path:
    if kind == "text":
        path = tmp_path / "notes.mp4"
        path.write_text("Giraffes and other words, nothing like a video.\n" * 20)
    elif kind == "empty":
        path = tmp_path / "empty.mp4"
        path.write_bytes(b"")
    elif kind == "jpeg":
        path = tmp_path / "photo.mp4"
        _ffmpeg("-i", str(H264), "-frames:v", "1", "-f", "image2", "-c:v", "mjpeg", str(path))
    else:  # audio only
        path = tmp_path / "audio.m4a"
        _ffmpeg("-i", str(H264), "-vn", "-c:a", "copy", str(path))
    return path


@pytest.mark.parametrize("kind", ["text", "empty", "jpeg", "audio"])
def test_not_a_video_is_unsupported_format(tmp_path, kind):
    output = tmp_path / "copy.mp4"
    _assert_error(_invoke(_make_not_a_video(tmp_path, kind), output), "unsupported_format", 21)
    assert not output.exists()


@pytest.mark.parametrize("faststart", [False, True], ids=["index-last", "index-first"])
def test_truncated_video_is_input_corrupt(tmp_path, faststart):
    """Cut in half: a phone writes its index (`moov`) last, so the index is lost; with the index first, the data."""
    whole = tmp_path / "whole.mp4"
    _ffmpeg("-i", str(HEVC_ROTATED), "-c", "copy", *(("-movflags", "+faststart") if faststart else ()), str(whole))
    video = tmp_path / "truncated.mp4"
    data = whole.read_bytes()
    video.write_bytes(data[: len(data) // 2])
    output = tmp_path / "copy.mp4"
    error = _assert_error(_invoke(video, output), "input_corrupt", 27)
    assert error["message"].startswith("The video can't be decoded")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["truncated.mp4", "whole.mp4"]


def test_existing_output_is_kept_without_overwrite(tmp_path):
    video = _copy(HEVC_ROTATED, tmp_path)
    output = tmp_path / "copy.mp4"
    output.write_bytes(b"previous")
    _assert_error(_invoke(video, output), "output_exists", 24)
    assert output.read_bytes() == b"previous"

    result = _display_copy(video, output, "--overwrite")
    assert result["output"]["path"] == str(output)
    assert _stream(output, "video")["codec_name"] == "h264"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["copy.mp4", "hevc_rotated.mp4"]


def test_existing_output_is_left_alone_when_no_copy_is_needed(tmp_path):
    video = _copy(H264, tmp_path)
    output = tmp_path / "copy.mp4"
    output.write_bytes(b"previous")
    assert _display_copy(video, output)["copy_needed"] is False
    assert output.read_bytes() == b"previous"


def test_copy_gets_the_permissions_of_a_plain_write(tmp_path):
    video = _copy(HEVC_ROTATED, tmp_path)
    output = tmp_path / "copy.mp4"
    umask = os.umask(0o027)
    try:
        _display_copy(video, output)
    finally:
        os.umask(umask)
    assert output.stat().st_mode & 0o777 == 0o640


@pytest.fixture(scope="module")
def long_hevc(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """An HEVC long enough (720p, 40 s) for its copy to last a few seconds, so that it is interrupted halfway."""
    path = tmp_path_factory.mktemp("long") / "long.mp4"
    _ffmpeg(
        *("-f", "lavfi", "-i", "testsrc2=s=1280x720:r=30:d=40", "-c:v", "libx265", "-preset", "ultrafast"),
        *("-x265-params", "log-level=error", str(path)),
    )
    return path


def _children(pid: int) -> list[int]:
    """The child processes of a process, from /proc."""
    children = []
    for task in Path(f"/proc/{pid}/task").iterdir():
        try:
            children += [int(child) for child in (task / "children").read_text().split()]
        except FileNotFoundError:  # a thread that just ended
            continue
    return children


def _wait_for_encoder(pid: int) -> int:
    """Wait until a process runs the copy's ffmpeg (libx264), and return its pid."""
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        for child in _children(pid):
            try:
                if "libx264" in Path(f"/proc/{child}/cmdline").read_text():
                    return child
            except FileNotFoundError:
                continue
        time.sleep(0.01)
    raise AssertionError("the copy's ffmpeg never started")


def _is_running(pid: int) -> bool:
    try:
        return Path(f"/proc/{pid}/stat").read_text().split(")")[-1].split()[0] != "Z"
    except FileNotFoundError:
        return False


@pytest.mark.parametrize(
    "sig", [signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGKILL], ids=lambda sig: sig.name
)
def test_interrupted_copy_leaves_nothing(tmp_path, long_hevc, sig):
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    output = output_dir / "new" / "copy.mp4"
    process = subprocess.Popen(
        [sys.executable, "-m", "compression_suite", "display-copy", str(long_hevc), "-o", str(output), "--json"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    encoder = _wait_for_encoder(process.pid)
    time.sleep(0.3)  # let it write
    process.send_signal(sig)
    stdout, _ = process.communicate(timeout=30)

    if sig is signal.SIGKILL:
        assert process.returncode == -signal.SIGKILL
    else:
        assert process.returncode == 130
        assert json.loads(stdout)["error"]["code"] == "interrupted"
    deadline = time.monotonic() + 1  # well before the copy would end on its own
    while _is_running(encoder) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not _is_running(encoder)  # ffmpeg doesn't keep encoding on its own
    if sig is not signal.SIGKILL:
        assert list(output_dir.iterdir()) == []  # neither a partial copy, nor a temporary file, nor `new`
    else:  # nothing runs after a SIGKILL: the folder created stays, empty
        assert [p.name for p in output_dir.rglob("*")] == ["new"]


# Review round 1: each test below reproduces a defect found in review.


@pytest.mark.parametrize("link", ["same", "hardlink", "symlink"])
@pytest.mark.parametrize("overwrite", [False, True], ids=["", "overwrite"])
def test_output_that_is_the_input_is_refused(tmp_path, link, overwrite):
    video = _copy(HEVC_ROTATED, tmp_path)
    before = video.read_bytes()
    output = tmp_path / "copy.mp4"
    if link == "same":
        output = video
    elif link == "hardlink":
        os.link(video, output)
    else:
        output.symlink_to(video)
    error = _assert_error(_invoke(video, output, *(["--overwrite"] if overwrite else [])), "usage_error", 2)
    assert "output is the input file" in error["message"]
    assert video.read_bytes() == before
    assert video.stat().st_mtime_ns == MTIME_NS


@pytest.mark.parametrize("source", [H264, HEVC_ROTATED], ids=["h264", "hevc"])
def test_audio_longer_than_the_video_is_not_corrupt(tmp_path, source):
    video = tmp_path / "long_audio.mp4"
    _ffmpeg(
        *("-i", str(source), "-f", "lavfi", "-i", "sine=duration=4", "-map", "0:v", "-map", "1:a"),
        *("-c:v", "copy", "-c:a", "aac", str(video)),
    )
    result = _display_copy(video, tmp_path / "copy.mp4")
    assert result["copy_needed"] is (source == HEVC_ROTATED)


@pytest.fixture(scope="module")
def transport_streams(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    """H.264 with B-frames and a single keyframe in MPEG-TS and M2TS, as a camcorder writes them: their timestamps
    start at 1.4 s, and a seek lands in the middle of the group of pictures."""
    folder = tmp_path_factory.mktemp("ts")
    encode = (
        *("-f", "lavfi", "-i", "testsrc2=s=320x240:r=25:d=4", "-f", "lavfi", "-i", "sine=duration=4"),
        *("-c:v", "libx264", "-preset", "veryfast", "-bf", "3", "-g", "250", "-c:a", "aac", "-shortest"),
    )
    paths = {"ts": folder / "video.ts", "m2ts": folder / "video.m2ts"}
    _ffmpeg(*encode, "-f", "mpegts", str(paths["ts"]))
    _ffmpeg(*encode, "-f", "mpegts", "-mpegts_m2ts_mode", "1", str(paths["m2ts"]))
    return paths


@pytest.mark.parametrize("kind", ["ts", "m2ts"])
def test_transport_streams_are_copied(tmp_path, transport_streams, kind):
    video = transport_streams[kind]
    output = tmp_path / "copy.mp4"
    result = _display_copy(video, output)
    assert (result["copy_needed"], result["copy_reasons"]) == (True, ["container mpegts"])
    assert result["duration_seconds"] == pytest.approx(4.0, abs=0.1)
    assert _stream(output, "video")["nb_read_frames"] == "100"


def _video_pts(video: Path) -> list[float]:
    """The presentation times of every video packet, in seconds, sorted."""
    output = subprocess.run(
        [str(resolve_tool("ffprobe")), "-v", "error", "-select_streams", "v:0", "-show_entries", "packet=pts_time"]
        + ["-of", "csv=p=0", str(video)],
        capture_output=True,
        check=True,
        text=True,
    ).stdout
    return sorted(float(line) for line in output.split())


def test_variable_frame_rate_keeps_every_timestamp(tmp_path):
    """A phone's frames don't come at an exact rate: their times, on a 90 kHz clock, must survive the copy."""
    video = tmp_path / "vfr.mp4"
    # 30 fps, but every third frame 7.3 ms late and every fifth one 3 ms early: irregular gaps, a burst included
    _ffmpeg(
        *("-f", "lavfi", "-i", "testsrc2=s=160x120:r=30:d=2"),
        *("-vf", "settb=1/90000,setpts='(N/30 + 0.0073*eq(mod(N\\,3)\\,2) - 0.003*eq(mod(N\\,5)\\,4))/TB'"),
        *("-c:v", "libx265", "-x265-params", "log-level=error", "-preset", "ultrafast"),
        *("-fps_mode", "passthrough", "-enc_time_base:v", "1:90000", "-video_track_timescale", "90000", str(video)),
    )
    original = _video_pts(video)
    assert len({round(b - a, 4) for a, b in zip(original, original[1:], strict=False)}) > 2  # really irregular
    assert _stream(video, "video")["r_frame_rate"] == "30/1"  # what ffmpeg would snap them to
    output = tmp_path / "copy.mp4"
    _display_copy(video, output)
    copied = _video_pts(output)
    assert len(copied) == len(original)
    assert max(abs(a - b) for a, b in zip(copied, original, strict=True)) < 0.001


def test_full_range_h264_needs_no_copy(tmp_path):
    video = tmp_path / "yuvj.mp4"
    _ffmpeg(
        "-i", str(H264), "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuvj420p", "-c:a", "copy", str(video)
    )
    assert _stream(video, "video")["pix_fmt"] == "yuvj420p"
    result = _display_copy(video, tmp_path / "copy.mp4")
    assert (result["copy_needed"], result["copy_reasons"]) == (False, [])


@pytest.mark.parametrize("codec", ["libx264", "libx265"])
def test_index_first_video_cut_short_is_input_corrupt(tmp_path, codec):
    """The index (first) still announces every frame, but the last few are gone: 4 % of the file cut off."""
    whole = tmp_path / "whole.mp4"
    _ffmpeg(
        *("-f", "lavfi", "-i", "testsrc2=s=320x240:r=30:d=6", "-c:v", codec, "-preset", "ultrafast", "-g", "30"),
        *("-x265-params", "log-level=error") if codec == "libx265" else (),
        *("-movflags", "+faststart", str(whole)),
    )
    video = tmp_path / "cut.mp4"
    data = whole.read_bytes()
    video.write_bytes(data[: len(data) * 96 // 100])
    output = tmp_path / "copy.mp4"
    error = _assert_error(_invoke(video, output), "input_corrupt", 27)
    assert "truncated" in error["message"]
    assert not output.exists()


def test_damaged_middle_is_input_corrupt_when_copied(tmp_path):
    """First and last frames decode, but the copy decodes everything: any decoder error makes the file damaged."""
    whole = tmp_path / "whole.mp4"
    _ffmpeg(
        *("-f", "lavfi", "-i", "testsrc2=s=320x240:r=30:d=4", "-c:v", "libx265", "-preset", "ultrafast"),
        *("-x265-params", "log-level=error:keyint=15", "-movflags", "+faststart", str(whole)),
    )
    data = bytearray(whole.read_bytes())
    middle = len(data) // 2
    data[middle : middle + 4000] = bytes(range(256)) * 15 + bytes(160)
    video = tmp_path / "damaged.mp4"
    video.write_bytes(bytes(data))
    output = tmp_path / "copy.mp4"
    _assert_error(_invoke(video, output), "input_corrupt", 27)
    assert not output.exists()


@pytest.mark.parametrize("name", ["concat:x.mov", "file:y.mov", "subfile,,start,0,end,0,,:x.mov"])
def test_relative_path_is_never_read_as_an_ffmpeg_protocol(tmp_path, monkeypatch, name):
    monkeypatch.chdir(tmp_path)
    _ffmpeg("-i", str(H264), "-c", "copy", "x.mov")  # what the protocol would read instead: playable
    shutil.copyfile(HEVC_ROTATED, tmp_path / name)
    result = _display_copy(Path(name), Path("copy.mp4"))
    assert (result["video_codec"], result["copy_needed"]) == ("hevc", True)
    assert _stream(tmp_path / "copy.mp4", "video")["codec_name"] == "h264"


def test_output_name_of_255_bytes_is_written(tmp_path):
    video = _copy(HEVC_ROTATED, tmp_path)
    output = tmp_path / ("é" * 125 + ".mp4")  # 254 bytes
    assert len(output.name.encode()) == 254
    assert _display_copy(video, output)["output"]["path"] == str(output)
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted([video.name, output.name])


def _hlg(tmp_path: Path) -> Path:
    video = tmp_path / "hdr_hlg.mp4"
    _ffmpeg(
        *("-i", str(HDR_PQ), "-c", "copy", "-bsf:v", "hevc_metadata=transfer_characteristics=18"),
        *("-color_trc", "arib-std-b67", str(video)),
    )
    return video


@pytest.mark.parametrize("transfer", ["smpte2084", "arib-std-b67"])
def test_hdr_is_tone_mapped_to_sdr(tmp_path, transfer):
    video = _copy(HDR_PQ, tmp_path) if transfer == "smpte2084" else _hlg(tmp_path)
    assert _stream(video, "video")["color_transfer"] == transfer
    output = tmp_path / "copy.mp4"
    result = _display_copy(video, output)
    assert result["copy_reasons"] == ["video codec hevc", f"hdr {transfer}"]
    copied = _stream(output, "video")
    assert (copied["codec_name"], copied["pix_fmt"]) == ("h264", "yuv420p")
    assert (copied["color_transfer"], copied["color_primaries"], copied["color_space"]) == ("bt709",) * 3
    _, _, pixels = _displayed_pixels(output)
    assert max(sum(pixel) for pixel in pixels) > 150  # tone-mapped, not black


def test_hdr_h264_is_copied(tmp_path):
    video = tmp_path / "hdr.mp4"
    _ffmpeg(
        *("-i", str(H264), "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "copy"),
        *("-x264-params", "colorprim=bt2020:transfer=smpte2084:colormatrix=bt2020nc", str(video)),
    )
    assert _stream(video, "video")["color_transfer"] == "smpte2084"
    result = _display_copy(video, tmp_path / "copy.mp4")
    assert (result["copy_needed"], result["copy_reasons"]) == (True, ["hdr smpte2084"])


def test_copied_video_stream_keeps_its_orientation(tmp_path):
    """Only the container is at fault: the H.264 stream is copied with its display matrix, shown upright."""
    video = tmp_path / "rotated.mkv"
    _ffmpeg("-display_rotation:v:0", "-90", "-i", str(H264), "-c", "copy", str(video))
    output = tmp_path / "copy.mp4"
    result = _display_copy(video, output)
    assert (result["copy_reasons"], result["width"], result["height"]) == (["container matroska,webm"], 72, 128)
    width, height, pixels = _displayed_pixels(output)
    assert (width, height) == (72, 128)
    _assert_colour(pixels[32 * width + 36], RED)
    _assert_colour(pixels[96 * width + 36], BLUE)
    _assert_colour(pixels[8 * width + 64], GREEN)


def test_copy_is_visually_lossless(tmp_path):
    """On a detailed picture, the copy stays within 45 dB of the decoded original (PSNR of the luma)."""
    video = tmp_path / "detailed.mp4"
    _ffmpeg(
        *("-f", "lavfi", "-i", "testsrc2=s=640x360:r=30:d=1", "-c:v", "libx265", "-preset", "ultrafast"),
        *("-x265-params", "log-level=error:lossless=1", str(video)),
    )
    output = tmp_path / "copy.mp4"
    _display_copy(video, output)
    log = subprocess.run(
        [str(resolve_tool("ffmpeg")), "-nostdin", "-i", str(output), "-i", str(video)]
        + ["-lavfi", "[0:v][1:v]psnr", "-f", "null", "-"],
        capture_output=True,
        check=True,
        text=True,
    ).stderr
    psnr_y = float(log.split("PSNR y:")[1].split()[0])
    assert psnr_y >= 45


# Review round 2


@pytest.mark.parametrize(
    ("name", "inputs", "video_codec"),
    [
        ("long_audio.mkv", ("-i", str(H264)), ("-c:v", "copy")),
        ("long_audio.webm", ("-i", str(H264)), ("-c:v", "libvpx-vp9")),
        ("late_video.mkv", ("-itsoffset", "1.5", "-i", str(H264)), ("-c:v", "copy")),
    ],
)
def test_matroska_streams_of_different_lengths_are_not_corrupt(tmp_path, name, inputs, video_codec):
    """Matroska gives no per-stream duration but a `DURATION` tag, the stream's end: the audio's end, or the video's
    start, must not make the video look truncated."""
    video = tmp_path / name
    _ffmpeg(
        *inputs,
        *("-f", "lavfi", "-i", "sine=duration=4", "-map", "0:v", "-map", "1:a", *video_codec, "-c:a", "libopus"),
        str(video),
    )
    output = tmp_path / "copy.mp4"
    result = _display_copy(video, output)
    assert result["copy_reasons"][0] == "container matroska,webm"
    assert _stream(output, "video")["nb_read_frames"] == "30"


@pytest.mark.parametrize(
    "retag",
    [
        # transfer only: primaries and matrix unspecified, in the bitstream and the container
        ("-bsf:v", "hevc_metadata=colour_primaries=2:matrix_coefficients=2", "-color_primaries", "unknown")
        + ("-colorspace", "unknown"),
        # constant-luminance matrix, which zscale doesn't take
        ("-bsf:v", "hevc_metadata=matrix_coefficients=10", "-colorspace", "bt2020c"),
        # colours in the container's `colr` box only, the bitstream untagged
        ("-bsf:v", "hevc_metadata=colour_primaries=2:transfer_characteristics=2:matrix_coefficients=2")
        + ("-color_primaries", "bt2020", "-color_trc", "smpte2084", "-colorspace", "bt2020nc"),
    ],
    ids=["transfer-only", "bt2020c", "colr-only"],
)
def test_hdr_with_incomplete_colour_tags_is_tone_mapped(tmp_path, retag):
    video = tmp_path / "pq.mp4"
    _ffmpeg("-i", str(HDR_PQ), "-c", "copy", *retag, str(video))
    assert _stream(video, "video")["color_transfer"] == "smpte2084"
    output = tmp_path / "copy.mp4"
    assert _display_copy(video, output)["copy_reasons"] == ["video codec hevc", "hdr smpte2084"]
    copied = _stream(output, "video")
    assert (copied["pix_fmt"], copied["color_transfer"], copied["color_primaries"]) == ("yuv420p", "bt709", "bt709")


def _damage_middle(path: Path) -> None:
    """Overwrite 8 KB in the middle of a file with garbage: its first and last frames still decode."""
    data = bytearray(path.read_bytes())
    middle = len(data) // 2
    data[middle : middle + 8192] = bytes(range(256)) * 32
    path.write_bytes(bytes(data))


def _garble_slices(path: Path) -> None:
    """Garble the slice data of three packets in the middle of the video stream, keeping each packet's 4-byte length
    prefix, NAL header and the start of the slice header: the container stays sound, only the decoder sees damage."""
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
    middle = len(packets) // 2
    for packet in packets[middle : middle + 3]:
        start, size = int(packet["pos"]), int(packet["size"])
        for i in range(start + 12, start + size):
            data[i] = (data[i] * 7 + 93) & 0xFF
    path.write_bytes(bytes(data))


@pytest.mark.parametrize(
    ("name", "audio"),
    [("pcm.mov", ("-c:a", "pcm_s16le")), ("video.mkv", ("-an",))],
    ids=["mov-pcm", "mkv"],
)
def test_copied_h264_damaged_in_the_middle_is_input_corrupt(tmp_path, name, audio):
    """Only the container or the audio is at fault: the H.264 stream is copied as is, but decoded all the same."""
    video = tmp_path / name
    _ffmpeg(
        *("-f", "lavfi", "-i", "testsrc2=s=320x240:r=30:d=4", "-f", "lavfi", "-i", "sine=duration=4"),
        *("-c:v", "libx264", "-preset", "ultrafast", "-g", "15", *audio, str(video)),
    )
    _garble_slices(video)
    output = tmp_path / "copy.mp4"
    error = _assert_error(_invoke(video, output), "input_corrupt", 27)
    assert "[h264 @" in error["message"]  # met by the decoder, not the demuxer
    assert not output.exists()


def test_failed_copy_removes_the_folders_it_created(tmp_path):
    video = tmp_path / "damaged.mp4"
    _ffmpeg(
        *("-f", "lavfi", "-i", "testsrc2=s=320x240:r=30:d=4", "-c:v", "libx265", "-preset", "ultrafast"),
        *("-x265-params", "log-level=error:keyint=15", "-movflags", "+faststart", str(video)),
    )
    _damage_middle(video)
    output = tmp_path / "new" / "nested" / "copy.mp4"
    _assert_error(_invoke(video, output), "input_corrupt", 27)
    assert not (tmp_path / "new").exists()


def _limit_file_size() -> None:
    """In the child: no file may grow past 4 KB, ffmpeg's output included (it then gets SIGXFSZ)."""
    resource.setrlimit(resource.RLIMIT_FSIZE, (4096, 4096))


def test_copy_failing_for_another_reason_removes_the_folders_it_created(tmp_path, long_hevc):
    (tmp_path / "existing").mkdir()
    output = tmp_path / "existing" / "new" / "nested" / "copy.mp4"
    completed = subprocess.run(
        [sys.executable, "-m", "compression_suite", "display-copy", str(long_hevc), "-o", str(output), "--json"],
        capture_output=True,
        text=True,
        preexec_fn=_limit_file_size,
    )
    assert completed.returncode == 50, completed.stdout
    assert json.loads(completed.stdout)["error"]["code"] == "internal_error"
    assert list((tmp_path / "existing").iterdir()) == []  # the folder that was there stays


# Review round 3


@pytest.mark.parametrize(
    "retag",
    [
        ("-bsf:v", "hevc_metadata=matrix_coefficients=0", "-colorspace", "rgb"),  # ffprobe's `gbr`, on YUV
        ("-bsf:v", "hevc_metadata=matrix_coefficients=11", "-colorspace", "smpte2085"),
        ("-bsf:v", "hevc_metadata=colour_primaries=10:matrix_coefficients=12")
        + ("-color_primaries", "smpte428", "-colorspace", "chroma-derived-nc"),
    ],
    ids=["gbr", "smpte2085", "smpte428-chroma-derived"],
)
def test_hdr_with_colours_zscale_doesnt_take_falls_back_to_bt2020(tmp_path, retag):
    video = tmp_path / "pq.mp4"
    _ffmpeg("-i", str(HDR_PQ), "-c", "copy", *retag, str(video))
    output = tmp_path / "copy.mp4"
    assert _display_copy(video, output)["copy_reasons"] == ["video codec hevc", "hdr smpte2084"]
    copied = _stream(output, "video")
    assert (copied["pix_fmt"], copied["color_transfer"], copied["color_primaries"]) == ("yuv420p", "bt709", "bt709")


@pytest.mark.parametrize("duration", [b"1.0".ljust(18), b"00:00:01,000000000"], ids=["seconds", "comma"])
def test_matroska_duration_tag_that_doesnt_parse_is_ignored(tmp_path, duration):
    """The tag is rewritten in place, at the same length: the video's end is then unknown, frames only have to come
    out."""
    video = tmp_path / "video.mkv"
    _ffmpeg("-i", str(H264), "-c", "copy", str(video))
    data = video.read_bytes()
    assert data.count(b"00:00:01.000000000") == 1  # the video's tag (its audio lasts a few ms more)
    video.write_bytes(data.replace(b"00:00:01.000000000", duration))
    assert _stream(video, "video")["tags"]["DURATION"].strip() == duration.decode().strip()
    result = _display_copy(video, tmp_path / "copy.mp4")
    assert result["copy_reasons"] == ["container matroska,webm"]


# --- One contract with the other video commands (#25) -----------------------------


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT, signal.SIGHUP], ids=lambda sig: sig.name)
def test_a_signal_while_the_video_is_probed_is_an_interruption(tmp_path, monkeypatch, sig):
    probe = display_copy_main.probe_video

    def probe_then_signal(*args):
        signal.raise_signal(sig)  # received while the video is probed, before any copy
        return probe(*args)

    def unhandled(received, frame):
        raise AssertionError(f"{signal.Signals(received).name} not handled by display-copy")  # not kill pytest

    monkeypatch.setattr(display_copy_main, "probe_video", probe_then_signal)
    previous = signal.signal(sig, unhandled)
    try:
        result = _invoke(_copy(HEVC_ROTATED, tmp_path), tmp_path / "new" / "copy.mp4")
    finally:
        signal.signal(sig, previous)
    _assert_error(result, "interrupted", 130)
    assert [path.name for path in tmp_path.iterdir()] == ["hevc_rotated.mp4"]


def test_a_file_appearing_at_the_output_during_the_copy_is_kept(tmp_path, monkeypatch):
    output = tmp_path / "copy.mp4"
    write = display_copy_main.write_with_ffmpeg

    def write_meanwhile(output_path, build_args, *args, **kwargs):
        def build_and_write(path):
            output.write_bytes(b"written meanwhile")
            return build_args(path)

        write(output_path, build_and_write, *args, **kwargs)

    monkeypatch.setattr(display_copy_main, "write_with_ffmpeg", write_meanwhile)
    _assert_error(_invoke(_copy(HEVC_ROTATED, tmp_path), output), "output_exists", 24)
    assert output.read_bytes() == b"written meanwhile"
    assert sorted(path.name for path in tmp_path.iterdir()) == ["copy.mp4", "hevc_rotated.mp4"]


def test_an_output_ending_with_a_slash_is_a_usage_error(tmp_path):
    video = _copy(HEVC_ROTATED, tmp_path)
    result = runner.invoke(app, ["display-copy", str(video), "-o", f"{tmp_path / 'newdir'}/", "--json"])
    _assert_error(result, "usage_error", 2)
    assert [path.name for path in tmp_path.iterdir()] == ["hevc_rotated.mp4"]


def test_a_dangling_symlink_at_the_output_is_kept(tmp_path):
    output = tmp_path / "copy.mp4"
    output.symlink_to(tmp_path / "nowhere.mp4")
    error = _assert_error(_invoke(_copy(HEVC_ROTATED, tmp_path), output), "output_exists", 24)
    assert "symbolic link" in error["message"]
    assert output.is_symlink()
    assert not (tmp_path / "nowhere.mp4").exists()
