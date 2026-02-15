"""Integration tests for compress-image — Tinify is mocked, exiftool runs for real."""

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from compression_suite.cli import app

runner = CliRunner()

TEST_IMAGE = Path(__file__).resolve().parent / "testfiles" / "logitech_keyboard.jpg"


def _mock_tinify():
    """Patch tinify so from_file/from_buffer return the real test image bytes."""
    mock = MagicMock()
    source = MagicMock()
    source.to_buffer.return_value = TEST_IMAGE.read_bytes()
    mock.from_file.return_value = source
    mock.from_buffer.return_value = source
    mock.compression_count = 42
    return patch("compression_suite.compress_image.main.tinify", mock)


def test_file_to_file_metadata_keep(tmp_path: Path):
    """Exiftool copies EXIF from original to compressed output."""
    out = tmp_path / "out.jpg"
    with _mock_tinify():
        result = runner.invoke(app, [
            "compress-image", str(TEST_IMAGE),
            "--output", str(out),
            "--api-key", "test-key",
            "--metadata", "keep",
        ])
    assert result.exit_code == 0, result.stderr
    assert out.read_bytes()[:2] == b"\xff\xd8"
    exif = subprocess.run(
        ["exiftool", "-Make", str(out)], capture_output=True, text=True,
    )
    assert "TestCamera" in exif.stdout


def test_stdin_to_file(tmp_path: Path):
    """Reading from stdin calls from_buffer and produces output."""
    out = tmp_path / "out.jpg"
    with _mock_tinify() as ctx:
        result = runner.invoke(app, [
            "compress-image",
            "--output", str(out),
            "--api-key", "test-key",
        ], input=TEST_IMAGE.read_bytes())
    assert result.exit_code == 0, result.stderr
    ctx.from_buffer.assert_called_once()
    assert out.exists()
