"""CLI tests for inspect — exiftool runs for real, Tinify must never be reached."""

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import Result
from typer.testing import CliRunner

from compression_suite.cli import app

runner = CliRunner()

SAMPLES = Path(__file__).resolve().parents[1] / "compress_image" / "samples"
S23_PHOTO = SAMPLES / "s23_p3_seft.jpg"
S23_MOTION_PHOTO = SAMPLES / "s23_motion_photo.jpg"
SAMSUNG_MOTION_PHOTO = SAMPLES / "samsung_motion_photo.jpg"
SCREENSHOT = SAMPLES / "screenshot.png"

IMPORTANT_KEYS = ["capture_date", "gps", "camera", "dimensions", "orientation", "color_profile", "transparency"]

# 2022-08-09T10:11:12.654321Z, in nanoseconds
MTIME_NS = 1_660_039_872_654_321_000
MTIME_ISO = "2022-08-09T10:11:12.654321+00:00"


def _copy(src: Path, tmp_path: Path, name: str | None = None) -> Path:
    """Copy a sample with a known mtime."""
    return _write(tmp_path, name or src.name, src.read_bytes())


def _write(tmp_path: Path, name: str, data: bytes) -> Path:
    """Write a file with a known mtime."""
    path = tmp_path / name
    path.write_bytes(data)
    os.utime(path, ns=(MTIME_NS, MTIME_NS))
    return path


def _invoke(path: Path, *args: str) -> Result:
    return runner.invoke(app, ["inspect", str(path), "--json", *args])


def _inspect(path: Path) -> dict:
    """Run inspect --json and return its `result`."""
    result = _invoke(path)
    assert result.exit_code == 0, result.output
    document = json.loads(result.stdout)
    assert document["schema_version"] == 1
    assert document["ok"] is True
    assert document["command"] == "inspect"
    return document["result"]


def _important(result: dict) -> dict[str, dict]:
    return {field["key"]: field for field in result["important"]}


# --- Images ---------------------------------------------------------------------


def test_s23_photo_is_described(tmp_path: Path):
    path = _copy(S23_PHOTO, tmp_path)
    result = _inspect(path)
    assert result["path"] == str(path)
    assert result["format"] == "jpeg"
    assert result["mime_type"] == "image/jpeg"
    assert result["size_bytes"] == S23_PHOTO.stat().st_size
    assert result["mtime"] == MTIME_ISO
    assert result["compressible"] is True
    assert result["motion_photo"] == {"detected": False, "signals": []}


def test_s23_photo_important_fields_have_a_value_and_a_source(tmp_path: Path):
    result = _inspect(_copy(S23_PHOTO, tmp_path))
    assert [field["key"] for field in result["important"]] == IMPORTANT_KEYS
    assert all(set(field) == {"key", "value", "source"} for field in result["important"])
    important = _important(result)
    assert important["capture_date"] == {
        "key": "capture_date", "value": "2024:06:15 12:00:00", "source": "ExifIFD:DateTimeOriginal",
    }
    assert important["camera"]["value"]["make"] == "samsung"
    assert important["gps"]["source"] == "GPS:GPSLatitude"
    assert important["color_profile"] == {
        "key": "color_profile",
        "value": "DCI-P3 D65 Gamut with sRGB Transfer",
        "source": "ICC_Profile:ProfileDescription",
    }
    assert important["transparency"] == {"key": "transparency", "value": False, "source": "pixels"}


def test_s23_photo_metadata_has_every_group_but_the_ignored_ones(tmp_path: Path):
    metadata = _inspect(_copy(S23_PHOTO, tmp_path))["metadata"]
    groups = {tag.split(":", 1)[0] for tag in metadata}
    assert {"File", "IFD0", "ExifIFD", "ICC_Profile", "Samsung"} <= groups
    assert not groups & {"System", "Composite", "ExifTool"}
    assert "SourceFile" not in metadata


def test_screenshot_capture_date_comes_from_the_mtime(tmp_path: Path):
    result = _inspect(_copy(SCREENSHOT, tmp_path))
    assert result["format"] == "png"
    assert result["mime_type"] == "image/png"
    assert result["compressible"] is True
    assert _important(result)["capture_date"] == {"key": "capture_date", "value": MTIME_ISO, "source": "file_mtime"}
    assert "PNG:Software" in result["metadata"]


@pytest.mark.parametrize(
    ("sample", "signals"),
    [
        (S23_MOTION_PHOTO, ["xmp_google_motion_photo", "xmp_container_video", "samsung_motion_photo_data"]),
        (SAMSUNG_MOTION_PHOTO, ["samsung_motion_photo_data"]),
    ],
)
def test_motion_photo_is_reported_without_failing(tmp_path: Path, sample: Path, signals: list[str]):
    result = _inspect(_copy(sample, tmp_path))
    assert result["motion_photo"] == {"detected": True, "signals": signals}
    assert result["compressible"] is True  # its format is accepted; compress-image refuses it for its video


# --- Other files ----------------------------------------------------------------


def test_text_file_is_described_by_exiftool(tmp_path: Path):
    result = _inspect(_write(tmp_path, "notes.txt", b"hello\n"))
    assert result["format"] == "txt"
    assert result["mime_type"] == "text/plain"
    assert result["size_bytes"] == 6
    assert result["compressible"] is False
    assert result["motion_photo"] == {"detected": False, "signals": []}
    important = _important(result)
    assert important["dimensions"] == {"key": "dimensions", "value": None, "source": None}
    assert important["transparency"] == {"key": "transparency", "value": None, "source": None}
    assert result["metadata"]["File:LineCount"] == 1


def test_pdf_is_not_compressible(tmp_path: Path):
    result = _inspect(_write(tmp_path, "doc.bin", b"%PDF-1.4\n%%EOF\n"))
    assert result["format"] == "pdf"
    assert result["mime_type"] == "application/pdf"
    assert result["compressible"] is False


def test_unknown_format_is_a_success(tmp_path: Path):
    result = _inspect(_write(tmp_path, "blob.dat", bytes(range(256)) * 4))
    assert result["format"] == "unknown"
    assert result["mime_type"] == "application/octet-stream"
    assert result["size_bytes"] == 1024
    assert result["compressible"] is False
    assert result["motion_photo"] == {"detected": False, "signals": []}
    assert _important(result)["capture_date"] == {"key": "capture_date", "value": MTIME_ISO, "source": "file_mtime"}
    assert not any(tag.startswith("ExifTool:") for tag in result["metadata"])


def test_empty_file_is_a_success(tmp_path: Path):
    result = _inspect(_write(tmp_path, "empty", b""))
    assert result["format"] == "unknown"
    assert result["size_bytes"] == 0


# --- Errors ---------------------------------------------------------------------


@pytest.mark.parametrize("missing_path", [lambda tmp: tmp / "missing.jpg", lambda tmp: tmp])
def test_missing_input_is_input_not_found(tmp_path: Path, missing_path):
    result = _invoke(missing_path(tmp_path))
    assert result.exit_code == 23, result.output
    assert json.loads(result.stdout)["error"]["code"] == "input_not_found"


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any file")
def test_unreadable_input_is_input_unreadable(tmp_path: Path):
    path = _copy(S23_PHOTO, tmp_path)
    path.chmod(0)
    result = _invoke(path)
    assert result.exit_code == 26, result.output
    assert json.loads(result.stdout)["error"]["code"] == "input_unreadable"


def test_text_named_like_a_jpeg_is_not_compressible(tmp_path: Path):
    result = _inspect(_write(tmp_path, "photo.jpg", b"hello\n"))
    assert result["format"] == "txt"
    assert result["compressible"] is False


def test_missing_exiftool_is_tool_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("COMPRESSION_SUITE_TOOLS_DIR", str(tmp_path / "no-tools"))
    result = _invoke(_copy(S23_PHOTO, tmp_path))
    assert result.exit_code == 30, result.output
    error = json.loads(result.stdout)["error"]
    assert error["code"] == "tool_missing"
    assert error["details"]["install_command"] == "just install-tools"


def test_missing_file_argument_is_a_json_usage_error():
    result = runner.invoke(app, ["inspect", "--json"])
    assert result.exit_code == 2
    assert json.loads(result.stdout)["error"]["code"] == "usage_error"


# --- Tinify ---------------------------------------------------------------------


def test_inspect_never_reaches_tinify_and_needs_no_key(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    home = tmp_path / "home"  # no configuration file anywhere
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    with (
        patch("tinify.from_file") as from_file,
        patch("tinify.from_buffer") as from_buffer,
        patch("tinify.validate") as validate,
    ):
        result = _invoke(_copy(S23_PHOTO, tmp_path))
    assert result.exit_code == 0, result.output
    assert not from_file.called
    assert not from_buffer.called
    assert not validate.called


# --- Human-readable output ------------------------------------------------------


def test_without_json_prints_a_readable_report(tmp_path: Path):
    result = runner.invoke(app, ["inspect", str(_copy(S23_PHOTO, tmp_path))])
    assert result.exit_code == 0, result.output
    assert not result.stdout.lstrip().startswith("{")
    assert "image/jpeg" in result.stdout
    assert "Compressible: yes" in result.stdout
    assert "DCI-P3 D65 Gamut with sRGB Transfer" in result.stdout
    assert "ICC_Profile:ProfileDescription" in result.stdout


def test_without_json_names_the_motion_photo_signals(tmp_path: Path):
    result = runner.invoke(app, ["inspect", str(_copy(SAMSUNG_MOTION_PHOTO, tmp_path))])
    assert result.exit_code == 0, result.output
    assert "samsung_motion_photo_data" in result.stdout


def test_without_json_errors_go_to_stderr(tmp_path: Path):
    result = runner.invoke(app, ["inspect", str(tmp_path / "missing.jpg")])
    assert result.exit_code == 23
    assert "does not exist" in result.stderr
