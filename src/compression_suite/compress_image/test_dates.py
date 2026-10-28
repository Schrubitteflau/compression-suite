"""CLI tests for keeping the original's dates on the compressed file — Tinify is mocked, exiftool runs for real."""

import io
import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image
from typer.testing import CliRunner

from compression_suite.cli import app

runner = CliRunner()

# 2021-03-04T05:06:07.123456Z and 2022-08-09T10:11:12.654321Z, in nanoseconds
ORIGINAL_ATIME_NS = 1_614_834_367_123_456_000
ORIGINAL_MTIME_NS = 1_660_039_872_654_321_000


def _png_screenshot(path: Path) -> Path:
    """Write a small PNG without EXIF, dated like an old screenshot."""
    buf = io.BytesIO()
    Image.new("RGB", (32, 24), (30, 120, 200)).save(buf, format="PNG")
    path.write_bytes(buf.getvalue())
    os.utime(path, ns=(ORIGINAL_ATIME_NS, ORIGINAL_MTIME_NS))
    return path


def _fake_tinify():
    """Patch tinify so the compressed result is the input itself."""
    mock = MagicMock()
    mock.compression_count = 42

    def make_source(data: bytes):
        source = MagicMock()
        source.to_buffer.return_value = data
        return source

    mock.from_file.side_effect = lambda path: make_source(Path(path).read_bytes())
    mock.from_buffer.side_effect = make_source
    return patch("compression_suite.compress_image.main.tinify", mock)


def _invoke(*args: str, input: bytes | None = None):
    return runner.invoke(app, ["compress-image", *args, "--api-key", "test-key"], input=input)


@pytest.mark.parametrize("metadata", ["keep", "strip"])
@pytest.mark.parametrize("json_flag", [(), ("--json",)], ids=["human", "json"])
def test_output_gets_original_atime_and_mtime(tmp_path: Path, metadata: str, json_flag: tuple[str, ...]):
    src = _png_screenshot(tmp_path / "screenshot.png")
    out = tmp_path / "out" / "screenshot.png"
    with _fake_tinify():
        result = _invoke(str(src), "--output", str(out), "--metadata", metadata, *json_flag)
    assert result.exit_code == 0, result.output
    stat = out.stat()
    assert stat.st_mtime_ns == ORIGINAL_MTIME_NS
    assert stat.st_atime_ns == ORIGINAL_ATIME_NS


def test_json_reports_equal_input_and_output_mtime(tmp_path: Path):
    src = _png_screenshot(tmp_path / "screenshot.png")
    out = tmp_path / "out.png"
    with _fake_tinify():
        result = _invoke(str(src), "--output", str(out), "--json")
    assert result.exit_code == 0, result.output
    res = json.loads(result.stdout)["result"]
    assert res["input"]["mtime"] == "2022-08-09T10:11:12.654321+00:00"
    assert res["output"]["mtime"] == "2022-08-09T10:11:12.654321+00:00"


@pytest.mark.parametrize("metadata", ["keep", "strip"])
@pytest.mark.parametrize("json_flag", [(), ("--json",)], ids=["human", "json"])
def test_stdin_has_no_date_to_carry_over(tmp_path: Path, metadata: str, json_flag: tuple[str, ...]):
    src = _png_screenshot(tmp_path / "screenshot.png")
    out = tmp_path / "out.png"
    with _fake_tinify():
        result = _invoke("--output", str(out), "--metadata", metadata, *json_flag, input=src.read_bytes())
    assert result.exit_code == 0, result.output
    assert out.stat().st_mtime_ns != ORIGINAL_MTIME_NS
    if json_flag:
        res = json.loads(result.stdout)["result"]
        assert res["input"]["mtime"] is None
        assert res["output"]["mtime"] is not None
