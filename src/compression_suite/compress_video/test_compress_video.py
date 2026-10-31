"""CLI tests for compress-video: the pinned ffmpeg, ffprobe and exiftool run for real, on videos fabricated at test
time by `_make_video` (a phone-like HEVC with rotation, GPS, creation date and audio; a plain H.264)."""

import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import Result
from typer.testing import CliRunner

from compression_suite.cli import app
from compression_suite.compress_video import main
from compression_suite.utils.pinned_tools import resolve_tool

runner = CliRunner()

# 2021-03-04T05:06:07.123456Z and 2022-08-09T10:11:12.654321Z, in nanoseconds
ATIME_NS = 1_614_834_367_123_456_000
MTIME_NS = 1_660_039_872_654_321_000
CREATED = "2022:08:09 10:11:12"
GPS = "48.8584, 2.2945, 35"


def _tool(name: str) -> str:
    return str(resolve_tool(name))


def _make_video(
    path: Path,
    codec: str = "hevc",
    size: str = "64x32",
    duration: float = 1.0,
    rate: str = "30",
    audio: bool = True,
    phone_metadata: bool = True,
    jitter: bool = False,
) -> Path:
    """Fabricate a short video, dated MTIME_NS on disk.

    With `jitter`, every other frame comes 3 ms late (a phone's variable frame rate). With `phone_metadata`, the
    metadata of a phone video is written by exiftool as a Galaxy S23 writes it: creation dates of the movie and its
    tracks, GPS in the user data (`©xyz`), a 90° rotation in the track matrix, Android keys.
    """
    encoder = ["-c:v", "libx265", "-x265-params", "log-level=error", "-tag:v", "hvc1"]
    if codec == "h264":
        encoder = ["-c:v", "libx264", "-pix_fmt", "yuv420p"]
    if jitter:  # frames 3 ms late every other one, as a phone's capture clock gives them
        encoder += ["-vf", "settb=1/90000,setpts=(N/30+0.003*mod(N\\,2))/TB", "-fps_mode", "passthrough"]
        encoder += ["-enc_time_base:v", "1/90000"]
    sound = ["-f", "lavfi", "-i", f"sine=frequency=440:duration={duration}"] if audio else []
    subprocess.run(
        [
            _tool("ffmpeg"),
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"testsrc=size={size}:rate={rate}:duration={duration}",
        ]
        + sound
        + encoder
        + (["-c:a", "aac"] if audio else [])
        + [str(path)],
        check=True,
    )
    if phone_metadata:
        dates = [
            f"-{tag}={CREATED}+00:00" for tag in ("CreateDate", "ModifyDate", "TrackCreateDate", "MediaCreateDate")
        ]
        subprocess.run(
            [_tool("exiftool"), "-q", "-overwrite_original", "-api", "QuickTimeUTC", *dates]
            + [f"-UserData:GPSCoordinates={GPS}", "-Rotation=90", "-Keys:AndroidVersion=16", str(path)],
            check=True,
        )
    os.utime(path, ns=(ATIME_NS, MTIME_NS))
    return path


@pytest.fixture(scope="module")
def phone_video(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A phone-like HEVC video, made once: tests copy it with `_copy`."""
    return _make_video(tmp_path_factory.mktemp("samples") / "phone.mp4")


def _copy(sample: Path, folder: Path) -> Path:
    """A copy of a sample, dated ATIME_NS and MTIME_NS."""
    path = folder / sample.name
    path.write_bytes(sample.read_bytes())
    os.utime(path, ns=(ATIME_NS, MTIME_NS))
    return path


def _invoke(source: Path, output: Path, *args: str, preset: str = "default") -> Result:
    return runner.invoke(app, ["compress-video", str(source), "--preset", preset, "-o", str(output), "--json", *args])


def _compress(source: Path, output: Path, *args: str, preset: str = "default") -> dict:
    """Run compress-video --json and return its `result`."""
    result = _invoke(source, output, *args, preset=preset)
    assert result.exit_code == 0, result.output
    document = json.loads(result.stdout)
    assert (document["schema_version"], document["ok"], document["command"]) == (1, True, "compress-video")
    return document["result"]


def _probe(video: Path) -> dict:
    output = subprocess.run(
        [_tool("ffprobe"), "-v", "error", "-show_streams", "-show_format", "-of", "json", str(video)],
        capture_output=True,
        check=True,
        text=True,
    ).stdout
    return json.loads(output)


def _stream(video: Path, kind: str) -> dict:
    return next(stream for stream in _probe(video)["streams"] if stream["codec_type"] == kind)


def _tags(video: Path, *tags: str) -> dict:
    """Tags read by the pinned exiftool, by group and name (`Track1:Rotation`...), numbers as numbers."""
    output = subprocess.run(
        [_tool("exiftool"), "-j", "-n", "-G1", *(f"-{tag}" for tag in tags), str(video)],
        capture_output=True,
        check=True,
        text=True,
    ).stdout
    found = json.loads(output)[0]
    found.pop("SourceFile")
    return found


# --- Export ----------------------------------------------------------------------


def test_phone_video_is_encoded_in_hevc_and_opus_at_the_same_definition(phone_video: Path, tmp_path: Path):
    source = _copy(phone_video, tmp_path)
    output = tmp_path / "out.mp4"
    result = _compress(source, output)
    video, audio = _stream(output, "video"), _stream(output, "audio")
    assert (video["codec_name"], video["codec_tag_string"], video["width"], video["height"]) == ("hevc", "hvc1", 64, 32)
    assert (audio["codec_name"], audio["channels"]) == ("opus", 1)
    assert _probe(output)["format"]["format_name"].startswith("mov,mp4")
    assert result["preset"] == "default"
    assert result["crf"] == 32


def _frame_times(video: Path) -> list[str]:
    """The presentation time of every video frame, as stored."""
    output = subprocess.run(
        [
            _tool("ffprobe"),
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "packet=pts_time",
            "-of",
            "csv=p=0",
        ]
        + [str(video)],
        capture_output=True,
        check=True,
        text=True,
    ).stdout
    return sorted(output.split(), key=float)


def test_every_frame_keeps_its_time(tmp_path: Path):
    # Uneven times, not multiples of the frame rate's 1/30 s: rounded unless the source's time base is kept
    source = _make_video(tmp_path / "jitter.mp4", jitter=True, phone_metadata=False)
    output = tmp_path / "out.mp4"
    _compress(source, output)
    assert _frame_times(output) == _frame_times(source)
    assert _stream(output, "video")["r_frame_rate"] == _stream(source, "video")["r_frame_rate"]


def _displayed_frame(video: Path) -> bytes:
    """The first frame as a player shows it (rotation applied), in RGB."""
    return subprocess.run(
        [_tool("ffmpeg"), "-v", "error", "-i", str(video), "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24"]
        + ["pipe:"],
        capture_output=True,
        check=True,
    ).stdout


def test_creation_dates_gps_and_rotation_are_kept(phone_video: Path, tmp_path: Path):
    output = tmp_path / "out.mp4"
    _compress(_copy(phone_video, tmp_path), output)
    important = ("CreateDate", "TrackCreateDate", "MediaCreateDate", "GPSCoordinates", "Rotation", "AndroidVersion")
    expected = _tags(phone_video, *important)
    assert expected["QuickTime:CreateDate"] == CREATED  # the sample does carry them
    assert "UserData:GPSCoordinates" in expected
    assert expected["Composite:Rotation"] == 90
    assert _tags(output, *important) == expected


def test_video_is_displayed_the_same_way(phone_video: Path, tmp_path: Path):
    output = tmp_path / "out.mp4"
    _compress(_copy(phone_video, tmp_path), output)
    original, compressed = _displayed_frame(phone_video), _displayed_frame(output)
    assert len(compressed) == len(original) == 32 * 64 * 3
    # Same picture, give or take the encoding: a turned or mirrored one would differ by far more
    assert sum(abs(a - b) for a, b in zip(original, compressed, strict=True)) / len(original) < 12


def test_no_other_location_is_added(phone_video: Path, tmp_path: Path):
    output = tmp_path / "out.mp4"
    _compress(_copy(phone_video, tmp_path), output)
    assert "UserData:LocationInformation" not in _tags(output, "LocationInformation")


def test_output_gets_the_original_dates_and_the_original_is_untouched(phone_video: Path, tmp_path: Path):
    source = _copy(phone_video, tmp_path)  # not read before the export: its atime stays ATIME_NS
    output = tmp_path / "out.mp4"
    _compress(source, output)
    assert (output.stat().st_mtime_ns, output.stat().st_atime_ns) == (MTIME_NS, ATIME_NS)
    assert source.read_bytes() == phone_video.read_bytes()
    assert source.stat().st_mtime_ns == MTIME_NS


def test_result_describes_input_output_duration_and_settings(phone_video: Path, tmp_path: Path):
    source = _copy(phone_video, tmp_path)
    output = tmp_path / "out" / "phone.mp4"  # its folder is created
    result = _compress(source, output)
    elapsed = result.pop("elapsed_seconds")
    assert isinstance(elapsed, float) and elapsed >= 0
    assert result == {
        "input": {
            "path": str(source),
            "format": "mp4",
            "size_bytes": phone_video.stat().st_size,
            "mtime": "2022-08-09T10:11:12.654321+00:00",
        },
        "output": {
            "path": str(output),
            "format": "mp4",
            "size_bytes": output.stat().st_size,
            "mtime": "2022-08-09T10:11:12.654321+00:00",
        },
        "duration_seconds": pytest.approx(1.0, abs=0.05),
        "preset": "default",
        "crf": 32,
    }


def _x265_settings(video: Path) -> str:
    """The settings x265 writes in its stream (an SEI message), as text."""
    data = video.read_bytes()
    start = data.index(b"x265 (build")
    return data[start : data.index(b"\0", start)].decode("latin-1")


def test_another_crf_replaces_the_presets_own(phone_video: Path, tmp_path: Path):
    output = tmp_path / "out.mp4"
    result = _compress(_copy(phone_video, tmp_path), output, "--crf", "40", preset="screen")
    assert (result["preset"], result["crf"]) == ("screen", 40)
    assert " crf=40.0 " in _x265_settings(output)
    assert " preset=slow " not in _x265_settings(output)  # x265 names its settings, not the preset: see below
    assert " rd=4 " in _x265_settings(output)  # rd 4 is what `slow` sets (`medium`, ffmpeg's default, sets 3)


def test_preset_crf_is_used_by_default(phone_video: Path, tmp_path: Path):
    output = tmp_path / "out.mp4"
    _compress(_copy(phone_video, tmp_path), output, preset="screen")
    assert " crf=35.0 " in _x265_settings(output)


def test_dance_preset_drops_the_audio(phone_video: Path, tmp_path: Path):
    output = tmp_path / "out.mp4"
    result = _compress(_copy(phone_video, tmp_path), output, preset="dance")
    assert [stream["codec_type"] for stream in _probe(output)["streams"]] == ["video"]
    assert result["crf"] == 45


def test_h264_video_without_metadata_nor_audio(tmp_path: Path):
    source = _make_video(tmp_path / "plain.mp4", codec="h264", size="48x64", audio=False, phone_metadata=False)
    output = tmp_path / "out.mp4"
    _compress(source, output)
    video = _stream(output, "video")
    assert (video["codec_name"], video["width"], video["height"]) == ("hevc", 48, 64)
    assert [stream["codec_type"] for stream in _probe(output)["streams"]] == ["video"]


def test_overwrite_replaces_an_existing_output(phone_video: Path, tmp_path: Path):
    output = tmp_path / "out.mp4"
    output.write_bytes(b"old")
    _compress(_copy(phone_video, tmp_path), output, "--overwrite")
    assert _stream(output, "video")["codec_name"] == "hevc"


# --- Progress --------------------------------------------------------------------


def test_progress_goes_to_stderr_as_json_lines_ending_at_100(tmp_path: Path):
    source = _make_video(tmp_path / "long.mp4", size="320x240", duration=4.0, phone_metadata=False)
    output = tmp_path / "out.mp4"
    result = _invoke(source, output)
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["ok"] is True  # stdout: the envelope alone
    lines = [json.loads(line) for line in result.stderr.splitlines()]
    assert len(lines) >= 3
    assert all((line["schema_version"], line["command"]) == (1, "compress-video") for line in lines)
    progress = [line["progress"] for line in lines]
    assert progress[0] == {"encoded_seconds": 0.0, "duration_seconds": pytest.approx(4.0, abs=0.05), "percent": 0.0}
    assert progress[-1] == {
        "encoded_seconds": progress[0]["duration_seconds"],
        "duration_seconds": progress[0]["duration_seconds"],
        "percent": 100.0,
    }
    percents = [step["percent"] for step in progress]
    assert percents == sorted(percents)
    assert all(percent < 100 for percent in percents[:-1])
    assert any(0 < percent < 100 for percent in percents)


def test_without_json_progress_is_readable_and_stdout_empty(phone_video: Path, tmp_path: Path):
    output = tmp_path / "out.mp4"
    result = runner.invoke(
        app, ["compress-video", str(_copy(phone_video, tmp_path)), "--preset", "default", "-o", str(output)]
    )
    assert result.exit_code == 0, result.output
    assert result.stdout == ""
    assert "Compressed:" in result.stderr
    assert "reduction" in result.stderr


# --- Errors ----------------------------------------------------------------------


def _assert_error(result: Result, code: str, exit_code: int) -> dict:
    """Check a failure's envelope, error code and exit code, and return its `error`."""
    assert result.exit_code == exit_code, result.output
    document = json.loads(result.stdout)
    assert (document["ok"], document["command"]) == (False, "compress-video")
    assert document["error"]["code"] == code
    return document["error"]


def _files(folder: Path) -> set[str]:
    return {path.name for path in folder.iterdir()}


@pytest.mark.parametrize(
    ("name", "content", "detected"),
    [
        ("photo.jpg", b"\xff\xd8\xff\xe0" + bytes(60), "jpeg"),
        ("notes.mp4", b"not a video, despite its name\n", "unknown"),
    ],
)
def test_a_file_that_is_not_a_video_is_unsupported(tmp_path: Path, name: str, content: bytes, detected: str):
    source = tmp_path / name
    source.write_bytes(content)
    error = _assert_error(_invoke(source, tmp_path / "out.mp4"), "unsupported_format", 21)
    assert error["details"] == {"detected_format": detected}
    assert _files(tmp_path) == {name}


def test_audio_only_file_is_unsupported(tmp_path: Path):
    source = tmp_path / "voice.m4a"
    subprocess.run(
        [_tool("ffmpeg"), "-v", "error", "-f", "lavfi", "-i", "sine=duration=0.5", "-c:a", "aac", str(source)],
        check=True,
    )
    error = _assert_error(_invoke(source, tmp_path / "out.mp4"), "unsupported_format", 21)
    assert error["details"] == {"detected_format": "mp4"}


def test_matroska_video_is_accepted(tmp_path: Path):
    source = tmp_path / "clip.mkv"
    subprocess.run(
        [_tool("ffmpeg"), "-v", "error", "-f", "lavfi", "-i", "testsrc=size=64x32:duration=0.5", "-c:v", "libx264"]
        + [str(source)],
        check=True,
    )
    output = tmp_path / "out.mp4"
    assert _compress(source, output)["input"]["format"] == "matroska"
    assert _stream(output, "video")["codec_name"] == "hevc"


def _truncated(sample: Path, path: Path, faststart: bool) -> Path:
    """A video cut halfway, as an interrupted sync leaves it. Without `faststart`, the index (`moov`) is at the end,
    as a phone writes it, and is lost; with it, the index is complete but half the frames are missing."""
    full = path.with_name(f"full-{path.name}")
    movflags = ["-movflags", "+faststart"] if faststart else []
    subprocess.run([_tool("ffmpeg"), "-v", "error", "-i", str(sample), "-c", "copy", *movflags, str(full)], check=True)
    data = full.read_bytes()
    full.unlink()
    path.write_bytes(data[: len(data) // 2])
    return path


@pytest.mark.parametrize("faststart", [False, True], ids=["index-lost", "frames-missing"])
def test_a_truncated_video_is_corrupt_and_nothing_is_written(phone_video: Path, tmp_path: Path, faststart: bool):
    source = _truncated(phone_video, tmp_path / "cut.mp4", faststart)
    error = _assert_error(_invoke(source, tmp_path / "out.mp4"), "input_corrupt", 27)
    assert error["details"] == {"detected_format": "mp4"}
    assert _files(tmp_path) == {"cut.mp4"}


def test_an_existing_output_is_kept_without_overwrite(phone_video: Path, tmp_path: Path):
    output = tmp_path / "out.mp4"
    output.write_bytes(b"old")
    _assert_error(_invoke(_copy(phone_video, tmp_path), output), "output_exists", 24)
    assert output.read_bytes() == b"old"


def test_the_original_can_never_be_its_own_output(phone_video: Path, tmp_path: Path):
    source = _copy(phone_video, tmp_path)
    _assert_error(_invoke(source, source, "--overwrite"), "usage_error", 2)
    assert source.read_bytes() == phone_video.read_bytes()
    assert _files(tmp_path) == {source.name}


def test_a_missing_original_is_not_found(tmp_path: Path):
    _assert_error(_invoke(tmp_path / "moved.mp4", tmp_path / "out.mp4"), "input_not_found", 23)


@pytest.mark.parametrize(
    ("args", "details"),
    [
        (("--preset", "tiktok"), {"preset": "tiktok", "known_presets": ["default", "screen", "dance"]}),
        (("--preset", "default", "--crf", "52"), {"crf": 52, "crf_min": 0, "crf_max": 51}),
    ],
)
def test_unknown_preset_or_crf_out_of_bounds_is_a_usage_error(tmp_path: Path, args: tuple[str, ...], details: dict):
    source = tmp_path / "video.mp4"
    source.write_bytes(b"")
    result = runner.invoke(app, ["compress-video", str(source), *args, "-o", str(tmp_path / "out.mp4"), "--json"])
    assert _assert_error(result, "usage_error", 2)["details"] == details


def test_a_crf_that_is_not_an_integer_is_a_usage_error(tmp_path: Path):
    result = runner.invoke(
        app,
        ["compress-video", "in.mp4", "--preset", "default", "--crf", "high", "-o", str(tmp_path / "o.mp4"), "--json"],
    )
    _assert_error(result, "usage_error", 2)


def test_a_missing_pinned_tool_is_tool_missing(phone_video: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("COMPRESSION_SUITE_TOOLS_DIR", str(tmp_path / "tools"))
    error = _assert_error(_invoke(_copy(phone_video, tmp_path), tmp_path / "out.mp4"), "tool_missing", 30)
    assert error["details"]["install_command"] == "just install-tools"


def test_tools_of_the_path_are_never_run(phone_video: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("ffmpeg", "ffprobe", "exiftool"):  # would fail the command if it were run
        (bin_dir / name).write_text("#!/bin/sh\nexit 1\n")
        (bin_dir / name).chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    output = tmp_path / "out.mp4"
    _compress(_copy(phone_video, tmp_path), output)
    assert _stream(output, "video")["codec_name"] == "hevc"


# --- Interruption ----------------------------------------------------------------


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT], ids=["SIGTERM", "SIGINT"])
def test_an_interrupted_export_leaves_no_file(tmp_path: Path, signum: signal.Signals):
    # Long enough to be caught encoding: a few seconds at the `slow` speed
    source = _make_video(tmp_path / "long.mp4", size="640x480", duration=10.0, phone_metadata=False)
    output = tmp_path / "out.mp4"
    process = subprocess.Popen(
        [sys.executable, "-m", "compression_suite", "compress-video", str(source), "--preset", "default"]
        + ["-o", str(output), "--json"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stderr is not None
        for line in process.stderr:  # wait until it encodes, with its temporary file
            if json.loads(line)["progress"]["percent"] > 0:
                break
        assert len(_files(tmp_path)) == 2  # the source and the temporary file
        process.send_signal(signum)
        stdout, _ = process.communicate(timeout=30)
    finally:
        process.kill()
    assert process.returncode == 130
    assert json.loads(stdout)["error"]["code"] == "interrupted"
    assert _files(tmp_path) == {"long.mp4"}


# --- Review round 1 --------------------------------------------------------------


@pytest.mark.parametrize("name", ["2024-06-01T10:30:00.mp4", "concat:clip.mp4", "a:b/clip.mp4"])
def test_a_relative_path_with_a_colon_is_a_file_not_a_protocol(
    phone_video: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
):
    source = tmp_path / name
    source.parent.mkdir(exist_ok=True)
    source.write_bytes(phone_video.read_bytes())
    monkeypatch.chdir(tmp_path)
    output = Path("c:d") / "out.mp4"
    result = runner.invoke(app, ["compress-video", name, "--preset", "dance", "-o", str(output), "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["result"]["input"]["path"] == name
    assert _stream(tmp_path / output, "video")["codec_name"] == "hevc"


def _start_export(source: Path, output: Path, *args: str) -> subprocess.Popen:
    """Run compress-video --json in a process of its own, and return once it is encoding."""
    process = subprocess.Popen(
        [sys.executable, "-m", "compression_suite", "compress-video", str(source), "--preset", "default"]
        + ["-o", str(output), "--json", *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stderr is not None
    for line in process.stderr:
        if json.loads(line)["progress"]["percent"] > 0:
            return process
    raise AssertionError(f"compress-video never encoded: {process.communicate()}")


def test_a_file_appearing_at_the_output_during_the_export_is_kept(tmp_path: Path):
    source = _make_video(tmp_path / "long.mp4", size="640x480", duration=6.0, phone_metadata=False)
    output = tmp_path / "out.mp4"
    process = _start_export(source, output)
    try:
        output.write_bytes(b"written meanwhile")
        stdout, _ = process.communicate(timeout=120)
    finally:
        process.kill()
    assert process.returncode == 24
    assert json.loads(stdout)["error"]["code"] == "output_exists"
    assert output.read_bytes() == b"written meanwhile"
    assert _files(tmp_path) == {"long.mp4", "out.mp4"}


def test_sigterm_before_encoding_is_an_interruption(phone_video: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    probe = main.probe_video

    def probe_then_sigterm(*args: object) -> object:
        signal.raise_signal(signal.SIGTERM)  # received while the video is probed
        return probe(*args)  # ty: ignore[invalid-argument-type]

    def unhandled(signum: int, frame: object) -> None:
        raise AssertionError("SIGTERM not handled by compress-video")  # instead of killing pytest itself

    monkeypatch.setattr(main, "probe_video", probe_then_sigterm)
    previous = signal.signal(signal.SIGTERM, unhandled)
    try:
        result = _invoke(_copy(phone_video, tmp_path), tmp_path / "new" / "out.mp4")
    finally:
        signal.signal(signal.SIGTERM, previous)
    assert _assert_error(result, "interrupted", 130)
    assert _files(tmp_path) == {"phone.mp4"}


def _two_audio_tracks(path: Path) -> Path:
    """A video with two audio tracks (a commentary, for instance): 440 Hz mono, 880 Hz stereo."""
    subprocess.run(
        [_tool("ffmpeg"), "-v", "error", "-f", "lavfi", "-i", "testsrc=size=64x32:duration=1"]
        + ["-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-f", "lavfi", "-i", "sine=frequency=880:duration=1"]
        + ["-map", "0", "-map", "1", "-map", "2", "-c:v", "libx264", "-c:a", "aac", "-ac:a:1", "2", str(path)],
        check=True,
    )
    return path


def test_every_audio_track_is_kept_in_opus(tmp_path: Path):
    output = tmp_path / "out.mp4"
    _compress(_two_audio_tracks(tmp_path / "two.mp4"), output)
    audio = [stream for stream in _probe(output)["streams"] if stream["codec_type"] == "audio"]
    assert [(stream["codec_name"], stream["channels"]) for stream in audio] == [("opus", 1), ("opus", 2)]
    assert all(int(stream["bit_rate"]) < 48_000 for stream in audio)  # 32 kb/s each


def test_dance_preset_drops_every_audio_track(tmp_path: Path):
    output = tmp_path / "out.mp4"
    _compress(_two_audio_tracks(tmp_path / "two.mp4"), output, preset="dance")
    assert [stream["codec_type"] for stream in _probe(output)["streams"]] == ["video"]


def test_without_a_known_duration_the_last_progress_line_keeps_the_seconds_encoded(tmp_path: Path):
    source = tmp_path / "streamed.mkv"  # written to a pipe: Matroska without a duration
    with source.open("wb") as file:
        subprocess.run(
            [_tool("ffmpeg"), "-v", "error", "-f", "lavfi", "-i", "testsrc=size=64x32:duration=2", "-c:v", "libx264"]
            + ["-f", "matroska", "pipe:1"],
            stdout=file,
            check=True,
        )
    result = _invoke(source, tmp_path / "out.mp4")
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["result"]["duration_seconds"] is None
    last = json.loads(result.stderr.splitlines()[-1])["progress"]
    assert last["percent"] == 100.0
    assert last["duration_seconds"] is None
    assert last["encoded_seconds"] == pytest.approx(2.0, abs=0.1)


def test_a_failed_export_leaves_no_folder_it_created(phone_video: Path, tmp_path: Path):
    source = _truncated(phone_video, tmp_path / "cut.mp4", faststart=True)  # fails while encoding
    _assert_error(_invoke(source, tmp_path / "new" / "deeper" / "out.mp4"), "input_corrupt", 27)
    assert _files(tmp_path) == {"cut.mp4"}


# --- Review round 2 --------------------------------------------------------------


def _ffmpeg(*args: str) -> None:
    """Run the pinned ffmpeg to fabricate a sample."""
    subprocess.run([_tool("ffmpeg"), "-v", "error", "-y", *args], check=True)


TESTSRC = ("-f", "lavfi", "-i", "testsrc=size=64x32:rate=10:duration=1")
SINE = ("-f", "lavfi", "-i", "sine=duration=1")

DATE_TAGS = ("CreateDate", "ModifyDate", "TrackCreateDate", "TrackModifyDate", "MediaCreateDate", "MediaModifyDate")


def test_every_quicktime_date_of_the_original_is_kept(tmp_path: Path):
    source = tmp_path / "dated.mp4"
    _ffmpeg(*TESTSRC, *SINE, "-c:v", "libx264", "-c:a", "aac", str(source))
    # Modified after its creation (an edit on the phone), each track at its own date, one date never set (zero)
    subprocess.run(
        [_tool("exiftool"), "-q", "-overwrite_original", "-QuickTime:CreateDate=2022:08:09 10:11:12"]
        + ["-QuickTime:ModifyDate=2023:01:02 03:04:05", "-Track1:TrackCreateDate=2022:08:09 10:11:12"]
        + ["-Track1:TrackModifyDate=2023:01:02 03:04:06", "-Track1:MediaCreateDate=2022:08:09 10:11:13"]
        + ["-Track2:TrackCreateDate=2022:08:09 10:11:14", "-Track2:TrackModifyDate=2023:01:02 03:04:07"]
        + ["-Track2:MediaCreateDate=2022:08:09 10:11:15", "-Track2:MediaModifyDate=2023:01:02 03:04:08", str(source)],
        check=True,
    )
    output = tmp_path / "out.mp4"
    _compress(source, output)
    expected = _tags(source, *DATE_TAGS)
    assert expected["Track1:MediaModifyDate"] == "0000:00:00 00:00:00"
    assert _tags(output, *DATE_TAGS) == expected


def _container_sample(path: Path, container: str) -> Path:
    """A 1-second video with audio in a container ffmpeg writes, with codecs it accepts."""
    codecs = {
        "webm": ["-c:v", "libvpx-vp9", "-c:a", "libopus"],
        "avi": ["-c:v", "libx264", "-c:a", "pcm_s16le"],
        "3gp": ["-c:v", "libx264", "-c:a", "aac", "-ac", "1", "-ar", "16000"],
    }.get(container, ["-c:v", "libx264", "-c:a", "aac"])
    muxer = {"matroska": "matroska", "mpegts": "mpegts", "mov": "mov", "webm": "webm", "avi": "avi", "3gp": "3gp"}
    _ffmpeg(*TESTSRC, *SINE, *codecs, "-f", muxer[container], str(path))
    return path


@pytest.mark.parametrize("container", ["webm", "avi", "mpegts", "3gp", "mov", "matroska"])
def test_every_supported_container_is_exported(tmp_path: Path, container: str):
    source = _container_sample(tmp_path / f"clip.{container}", container)
    output = tmp_path / "out.mp4"
    result = _compress(source, output)
    assert result["input"]["format"] == container
    assert _stream(output, "video")["codec_name"] == "hevc"
    assert _stream(output, "audio")["codec_name"] == "opus"


@pytest.mark.parametrize("container", ["matroska", "webm"])
def test_a_truncated_matroska_is_corrupt_and_nothing_is_written(tmp_path: Path, container: str):
    full = _container_sample(tmp_path / "full.mkv", container)
    data = full.read_bytes()
    full.unlink()
    source = tmp_path / "cut.mkv"
    source.write_bytes(data[: len(data) // 2])  # second half gone: Matroska declares no index ffprobe needs first
    result = _invoke(source, tmp_path / "out.mp4")
    error = _assert_error(result, "input_corrupt", 27)
    assert error["details"] == {"detected_format": container}
    assert _files(tmp_path) == {"cut.mkv"}
    assert all(json.loads(line)["progress"]["percent"] < 100 for line in result.stderr.splitlines())


def _udta_first(source: Path, destination: Path) -> Path:
    """Copy an MP4, its `udta` box moved first in `moov`: ffmpeg then reads the cover art (`covr`) it holds as stream
    0, before the tracks. The boxes keep their sizes, so no offset changes."""

    def boxes(data: bytes, start: int, end: int) -> list[tuple[bytes, int, int]]:
        found = []
        while start < end:
            size = int.from_bytes(data[start : start + 4], "big")
            found.append((data[start + 4 : start + 8], start, start + size))
            start += size
        return found

    data = source.read_bytes()
    _, start, end = next(box for box in boxes(data, 0, len(data)) if box[0] == b"moov")
    children = sorted(boxes(data, start + 8, end), key=lambda box: box[0] != b"udta")
    destination.write_bytes(data[: start + 8] + b"".join(data[s:e] for _, s, e in children) + data[end:])
    return destination


def test_a_cover_before_the_video_is_not_what_gets_encoded(tmp_path: Path):
    cover = tmp_path / "cover.png"
    _ffmpeg("-f", "lavfi", "-i", "color=c=red:size=40x40", "-frames:v", "1", str(cover))
    video = tmp_path / "video.mp4"
    _ffmpeg(*TESTSRC, "-c:v", "libx264", str(video))
    subprocess.run([_tool("exiftool"), "-q", "-overwrite_original", f"-CoverArt<={cover}", str(video)], check=True)
    source = _udta_first(video, tmp_path / "covered.mp4")
    first = _probe(source)["streams"][0]
    assert (first["codec_name"], first["disposition"]["attached_pic"]) == ("png", 1)
    output = tmp_path / "out.mp4"
    result = _compress(source, output)
    encoded = _stream(output, "video")
    assert (encoded["width"], encoded["height"], encoded["nb_frames"]) == (64, 32, "10")
    assert result["duration_seconds"] == pytest.approx(1.0, abs=0.05)


@pytest.mark.parametrize(
    ("layout", "expected"),
    [("4.0", "quad"), ("2.1", "stereo"), ("hexagonal", "5.1"), ("5.1(side)", "5.1"), ("stereo", "stereo")],
)
def test_audio_opus_can_not_hold_is_downmixed_keeping_most_channels(tmp_path: Path, layout: str, expected: str):
    source = tmp_path / "surround.mov"  # PCM in a MOV: any layout fits
    _ffmpeg(
        *TESTSRC,
        *("-f", "lavfi", "-i", f"sine=duration=1,aformat=channel_layouts={layout}"),
        *("-c:v", "libx264", "-c:a", "pcm_s16le", str(source)),
    )
    output = tmp_path / "out.mp4"
    _compress(source, output)
    audio = _stream(output, "audio")
    assert (audio["codec_name"], audio["channel_layout"]) == ("opus", expected)


def test_a_container_compress_video_does_not_read_is_named(tmp_path: Path):
    source = tmp_path / "clip.mpg"
    _ffmpeg(*TESTSRC, "-c:v", "mpeg2video", "-f", "mpeg", str(source))
    error = _assert_error(_invoke(source, tmp_path / "out.mp4"), "unsupported_format", 21)
    assert error["details"] == {"detected_format": "mpeg"}
    assert "video container mpeg not supported" in error["message"]
    assert "mp4, mov, 3gp, matroska, webm, avi, mpegts" in error["message"]


def test_a_second_video_stream_is_refused(tmp_path: Path):
    still = tmp_path / "still.png"
    _ffmpeg("-f", "lavfi", "-i", "color=c=red:size=40x40", "-frames:v", "1", str(still))
    source = tmp_path / "two.mkv"  # a still PNG track, then the video: two video tracks, none a cover
    _ffmpeg("-i", str(still), *TESTSRC, "-map", "0", "-map", "1", "-c:v:0", "copy", "-c:v:1", "libx264", str(source))
    error = _assert_error(_invoke(source, tmp_path / "out.mp4"), "unsupported_format", 21)
    assert error["details"] == {"detected_format": "matroska"}
    assert "holds 2 video streams, only one is supported" in error["message"]
    assert _files(tmp_path) == {"still.png", "two.mkv"}


def _hold_unhandled(signum: signal.Signals) -> object:
    """Make a signal compress-video doesn't handle fail the test instead of killing pytest itself."""

    def unhandled(received: int, frame: object) -> None:
        raise AssertionError(f"{signal.Signals(received).name} not handled by compress-video")

    return signal.signal(signum, unhandled)


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT], ids=["SIGTERM", "SIGINT"])
def test_a_signal_once_the_output_is_in_place_still_reports_the_export(
    phone_video: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, signum: signal.Signals
):
    put_in_place = main.put_in_place

    def put_in_place_then_signal(*args: object) -> None:
        put_in_place(*args)  # ty: ignore[invalid-argument-type]
        signal.raise_signal(signum)  # received between the rename and the envelope

    monkeypatch.setattr(main, "put_in_place", put_in_place_then_signal)
    output = tmp_path / "out.mp4"
    previous = _hold_unhandled(signum)
    try:
        result = _invoke(_copy(phone_video, tmp_path), output)
    finally:
        signal.signal(signum, previous)
    # Never a mix: the output is on disk, so the export is reported done
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["ok"] is True
    assert _stream(output, "video")["codec_name"] == "hevc"


def test_a_dangling_symlink_at_the_output_is_refused_before_encoding(phone_video: Path, tmp_path: Path):
    output = tmp_path / "out.mp4"
    output.symlink_to(tmp_path / "nowhere.mp4")
    result = _invoke(_copy(phone_video, tmp_path), output)
    error = _assert_error(result, "output_exists", 24)
    assert "symbolic link" in error["message"]
    assert result.stderr == ""  # refused before encoding: no progress
    assert output.is_symlink()


def test_an_output_ending_with_a_slash_is_a_usage_error(phone_video: Path, tmp_path: Path):
    output = f"{tmp_path / 'newdir'}/"
    result = runner.invoke(
        app, ["compress-video", str(_copy(phone_video, tmp_path)), "--preset", "default", "-o", output, "--json"]
    )
    _assert_error(result, "usage_error", 2)
    assert _files(tmp_path) == {"phone.mp4"}


def test_human_progress_shows_the_seconds_encoded_out_of_the_duration(tmp_path: Path):
    source = _make_video(tmp_path / "long.mp4", size="320x240", duration=4.0, phone_metadata=False)
    result = runner.invoke(app, ["compress-video", str(source), "--preset", "default", "-o", str(tmp_path / "o.mp4")])
    assert result.exit_code == 0, result.output
    assert "Encoding" in result.stderr
    assert "4.0/4.0 s" in result.stderr


# --- Review round 3 --------------------------------------------------------------


def _run_cli(source: Path, output: Path) -> subprocess.Popen:
    """Run compress-video --json in a process of its own."""
    return subprocess.Popen(
        [sys.executable, "-m", "compression_suite", "compress-video", str(source), "--preset", "dance"]
        + ["-o", str(output), "--json"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT], ids=["SIGTERM", "SIGINT"])
def test_a_signal_right_after_the_envelope_keeps_its_exit_code(tmp_path: Path, signum: signal.Signals):
    source = _make_video(tmp_path / "short.mp4", size="32x32", duration=0.2, audio=False, phone_metadata=False)
    codes = []
    for attempt in range(6):  # the process is shutting down: it would die of the signal, some of the times
        output = tmp_path / f"out-{attempt}.mp4"
        process = _run_cli(source, output)
        assert process.stdout is not None
        envelope = json.loads(process.stdout.readline())
        process.send_signal(signum)
        process.wait(timeout=30)
        assert envelope["ok"] is True
        codes.append(process.returncode)
    assert codes == [0] * 6


def test_signals_during_the_cleanup_of_an_interruption_change_nothing(tmp_path: Path):
    source = _make_video(tmp_path / "long.mp4", size="640x480", duration=10.0, phone_metadata=False)
    process = _start_export(source, tmp_path / "out.mp4")
    try:
        for _ in range(5):  # a caller insisting: the first one interrupts, the others must not break the cleanup
            process.send_signal(signal.SIGTERM)
        assert process.stdout is not None
        envelope = json.loads(process.stdout.readline())
        process.send_signal(signal.SIGTERM)
        process.wait(timeout=30)
    finally:
        process.kill()
    assert envelope["error"]["code"] == "interrupted"
    assert process.returncode == 130
    assert _files(tmp_path) == {"long.mp4"}
