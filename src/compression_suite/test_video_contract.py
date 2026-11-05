"""Contract tests of the three video commands WelcomeApp calls (display-copy, preview-portion, compress-video): for the
same video, their common fields have the same names, shapes and values, and the same error gets the same code,
message and details. The pinned tools run for real, on the fabricated samples."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from compression_suite.cli import app
from compression_suite.utils.pinned_tools import resolve_tool
from compression_suite.video_samples import HEVC_ROTATED

runner = CliRunner()

# 2022-08-09T10:11:12.654321Z, in nanoseconds
MTIME_NS = 1_660_039_872_654_321_000
MTIME_ISO = "2022-08-09T10:11:12.654321+00:00"

COMMANDS = ("display-copy", "preview-portion", "compress-video")
FILE_FIELDS = {"path", "format", "size_bytes", "mtime"}


def _copy(source: Path, folder: Path, name: str | None = None) -> Path:
    path = folder / (name or source.name)
    shutil.copyfile(source, path)
    os.utime(path, ns=(MTIME_NS, MTIME_NS))
    return path


def _ffmpeg(*args: str) -> None:
    subprocess.run([str(resolve_tool("ffmpeg")), "-v", "error", "-nostdin", "-y", *args], check=True)


def _arguments(command: str, video: Path, output: Path) -> list[str]:
    """The command line of each command on a video, with the same preset for the two that encode."""
    if command == "preview-portion":
        return [command, str(video), "--preset", "default", "--start", "0", "-o", str(output), "--json"]
    if command == "compress-video":
        return [command, str(video), "--preset", "default", "-o", str(output), "--json"]
    return [command, str(video), "-o", str(output), "--json"]


def _run(command: str, video: Path, output: Path) -> dict:
    """Run a command --json and return its whole document."""
    result = runner.invoke(app, _arguments(command, video, output))
    document = json.loads(result.stdout)
    assert document["schema_version"] == 2
    assert document["command"] == command
    return document


@pytest.fixture(scope="module")
def results(tmp_path_factory: pytest.TempPathFactory) -> dict[str, dict]:
    """The `result` of each command on the same video, the rotated HEVC sample."""
    folder = tmp_path_factory.mktemp("contract")
    video = _copy(HEVC_ROTATED, folder)
    found = {}
    for command in COMMANDS:
        document = _run(command, video, folder / f"{command}.mp4")
        assert document["ok"] is True, document
        found[command] = document["result"]
    return found


def test_the_input_is_described_alike(results: dict[str, dict], tmp_path: Path):
    inputs = [results[command]["input"] for command in COMMANDS]
    assert inputs[0].keys() == FILE_FIELDS
    assert inputs[0]["format"] == "mp4"
    assert inputs[0]["size_bytes"] == HEVC_ROTATED.stat().st_size
    assert inputs[0]["mtime"] == MTIME_ISO
    assert inputs[0]["path"].endswith("/hevc_rotated.mp4")
    assert inputs[1] == inputs[0]
    assert inputs[2] == inputs[0]


def test_the_output_is_described_alike(results: dict[str, dict]):
    for command in COMMANDS:
        output = results[command]["output"]
        assert output.keys() == FILE_FIELDS, command
        assert output["path"].endswith(f"/{command}.mp4")
        assert output["format"] == "mp4"
        assert output["size_bytes"] == Path(output["path"]).stat().st_size


def test_the_duration_of_the_video_is_the_same(results: dict[str, dict]):
    durations = [results[command]["duration_seconds"] for command in COMMANDS]
    assert durations[0] == pytest.approx(1.0, abs=0.05)
    assert durations == [durations[0]] * 3
    assert all("duration" not in results[command] for command in COMMANDS)


def test_preset_crf_and_elapsed_time_are_named_alike(results: dict[str, dict]):
    portion, export = results["preview-portion"], results["compress-video"]
    assert (portion["preset"], portion["crf"]) == (export["preset"], export["crf"]) == ("default", 32)
    for command in COMMANDS:
        assert isinstance(results[command]["elapsed_seconds"], float), command


def _expect_same_error(video: Path, folder: Path, code: str) -> dict:
    """Run the three commands on a video that fails, and return the error they all give."""
    errors = [_run(command, video, folder / f"{command}.mp4")["error"] for command in COMMANDS]
    assert errors[0]["code"] == code
    assert errors[1] == errors[0]
    assert errors[2] == errors[0]
    assert sorted(path.name for path in folder.iterdir()) == [video.name]
    return errors[0]


def test_an_image_is_refused_alike(tmp_path: Path):
    image = tmp_path / "photo.mp4"
    image.write_bytes(b"\xff\xd8\xff\xe0" + bytes(60))
    error = _expect_same_error(image, tmp_path, "unsupported_format")
    assert error["details"] == {"detected_format": "jpeg"}


def test_an_audio_only_file_is_refused_alike(tmp_path: Path):
    audio = tmp_path / "voice.m4a"
    _ffmpeg("-f", "lavfi", "-i", "sine=duration=0.5", "-c:a", "aac", str(audio))
    error = _expect_same_error(audio, tmp_path, "unsupported_format")
    assert error["details"] == {"detected_format": "mp4"}


def test_a_video_cut_before_its_index_is_damaged_alike(tmp_path: Path):
    cut = tmp_path / "cut.mp4"
    cut.write_bytes(HEVC_ROTATED.read_bytes()[: HEVC_ROTATED.stat().st_size // 2])
    error = _expect_same_error(cut, tmp_path, "input_corrupt")
    assert error["details"] == {"detected_format": "mp4"}
    assert error["message"].startswith("The video can't be decoded, the file is damaged")


@pytest.mark.parametrize(
    ("container", "codecs", "detected"),
    [("flv", ("-c:v", "libx264", "-c:a", "aac"), "flv"), ("ogg", ("-c:v", "libtheora", "-c:a", "libopus"), "ogg")],
)
def test_rare_containers_are_read_by_all_three(tmp_path: Path, container: str, codecs: tuple[str, ...], detected: str):
    video = tmp_path / f"clip.{container}"
    _ffmpeg(
        *("-f", "lavfi", "-i", "testsrc=size=64x32:rate=10:duration=1", "-f", "lavfi", "-i", "sine=duration=1"),
        *codecs,
        *("-f", container, str(video)),
    )
    for command in COMMANDS:
        document = _run(command, video, tmp_path / f"{command}.mp4")
        assert document["ok"] is True, document
        assert document["result"]["input"]["format"] == detected


@pytest.mark.parametrize("name", ["readme.nfo", "art.ans"])
def test_a_text_file_ffmpeg_reads_as_text_is_refused_alike(tmp_path: Path, name: str):
    text = tmp_path / name
    text.write_text("Not a video, though ffmpeg's tty demuxer reads it as one\n" * 20)
    error = _expect_same_error(text, tmp_path, "unsupported_format")
    assert error["details"] == {"detected_format": "unknown"}


@pytest.mark.parametrize(("command", "runs"), [("display-copy", 1), ("preview-portion", 2), ("compress-video", 1)])
def test_each_ffmpeg_run_is_announced_on_stderr_under_json(tmp_path: Path, command: str, runs: int):
    video = _copy(HEVC_ROTATED, tmp_path)
    result = runner.invoke(app, _arguments(command, video, tmp_path / "out.mp4"))
    assert json.loads(result.stdout)["ok"] is True  # stdout: the envelope alone
    announced = [json.loads(line) for line in result.stderr.splitlines() if "tool_command" in line]
    assert len(announced) == runs
    assert all((line["schema_version"], line["command"]) == (2, command) for line in announced)
    for line in announced:
        assert line["tool_command"][0] == str(resolve_tool("ffmpeg"))
    assert str(video.absolute()) in " ".join(announced[0]["tool_command"])


def test_nothing_is_announced_without_json(tmp_path: Path):
    video = _copy(HEVC_ROTATED, tmp_path)
    arguments = [arg for arg in _arguments("display-copy", video, tmp_path / "out.mp4") if arg != "--json"]
    result = runner.invoke(app, arguments)
    assert result.exit_code == 0, result.output
    assert "tool_command" not in result.stderr
