"""Integration tests for reduce-jpeg-size — jpegoptim runs for real."""

from pathlib import Path

from typer.testing import CliRunner

from compression_suite.cli import app

runner = CliRunner()

TEST_IMAGE = Path(__file__).resolve().parent / "testfiles" / "logitech_keyboard.jpg"


def test_file_to_file(tmp_path: Path):
    """Output is a valid JPEG within the target size."""
    out = tmp_path / "out.jpg"
    max_size_kb = 500
    result = runner.invoke(app, [
        "reduce-jpeg-size", str(TEST_IMAGE),
        "--output", str(out),
        "--max-size", str(max_size_kb),
    ])
    assert result.exit_code == 0, result.stderr
    data = out.read_bytes()
    assert data[:2] == b"\xff\xd8"
    assert len(data) <= max_size_kb * 1024


def test_stdin_to_stdout():
    """Piping bytes through stdin produces a valid JPEG on stdout."""
    result = runner.invoke(app, [
        "reduce-jpeg-size",
        "--max-size", "2000",
    ], input=TEST_IMAGE.read_bytes())
    assert result.exit_code == 0, result.stderr
    assert result.stdout_bytes[:2] == b"\xff\xd8"


def test_verbose_shows_version(tmp_path: Path):
    out = tmp_path / "out.jpg"
    result = runner.invoke(app, [
        "reduce-jpeg-size", str(TEST_IMAGE),
        "--output", str(out),
        "--verbose",
    ])
    assert result.exit_code == 0, result.stderr
    assert "jpegoptim version:" in result.stderr
