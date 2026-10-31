"""CLI tests for preview — pillow-heif decodes for real, nothing is mocked."""

import json
import os
from pathlib import Path
from unittest.mock import patch

import pillow_heif
import pytest
from click.testing import Result
from PIL import Image
from typer.testing import CliRunner

from compression_suite.cli import app

runner = CliRunner()

SAMPLES = Path(__file__).resolve().parents[1] / "compress_image" / "samples"
ORIENTED_P3_HEIC = SAMPLES / "oriented_p3.heic"
PLAIN_HEIC = SAMPLES / "plain.heic"
TILED_HEIC = SAMPLES / "tiled.heic"
OVER_LIMIT_HEIC = SAMPLES / "over_limit.heic"
S23_PHOTO = SAMPLES / "s23_p3_seft.jpg"

# 2022-08-09T10:11:12.654321Z, in nanoseconds
MTIME_NS = 1_660_039_872_654_321_000

RED = (220, 40, 40)
BLUE = (40, 40, 220)
GREEN = (40, 200, 40)


def _copy(src: Path, tmp_path: Path) -> Path:
    """Copy a sample with a known mtime."""
    path = tmp_path / src.name
    path.write_bytes(src.read_bytes())
    os.utime(path, ns=(MTIME_NS, MTIME_NS))
    return path


def _invoke(path: Path, output: Path, *args: str) -> Result:
    return runner.invoke(app, ["preview", str(path), "-o", str(output), "--json", *args])


def _preview(path: Path, output: Path, *args: str) -> dict:
    """Run preview --json and return its `result`."""
    result = _invoke(path, output, *args)
    assert result.exit_code == 0, result.output
    document = json.loads(result.stdout)
    assert document["schema_version"] == 1
    assert document["ok"] is True
    assert document["command"] == "preview"
    return document["result"]


def _assert_error(result: Result, code: str, exit_code: int) -> dict:
    """Check a failure's envelope, error code and exit code, and return its `error`."""
    assert result.exit_code == exit_code, result.output
    document = json.loads(result.stdout)
    assert document["ok"] is False
    assert document["command"] == "preview"
    assert document["error"]["code"] == code
    return document["error"]


def _close_to(pixel: tuple[int, ...], color: tuple[int, int, int]) -> bool:
    """Whether a decoded pixel matches a color, give or take the JPEG and HEVC losses."""
    return all(abs(a - b) <= 30 for a, b in zip(pixel, color, strict=True))


# --- Conversion -----------------------------------------------------------------


def test_heic_is_converted_to_a_full_resolution_jpeg(tmp_path: Path):
    heic = _copy(ORIENTED_P3_HEIC, tmp_path)
    output = tmp_path / "preview.jpg"
    result = _preview(heic, output)
    assert result == {
        "input": str(heic),
        "output": str(output),
        "width": 32,
        "height": 64,
        "size_bytes": output.stat().st_size,
    }
    with Image.open(output) as jpeg:
        assert jpeg.format == "JPEG"
        assert jpeg.size == (32, 64)


def test_orientation_is_applied_to_the_pixels(tmp_path: Path):
    output = tmp_path / "preview.jpg"
    _preview(_copy(ORIENTED_P3_HEIC, tmp_path), output)
    with Image.open(output) as jpeg:
        assert "exif" not in jpeg.info  # no orientation left for a viewer to apply twice
        # Mirror + rotation: red top, blue bottom, green square still top-left (a rotation alone moves it)
        assert _close_to(jpeg.getpixel((2, 2)), GREEN)
        assert _close_to(jpeg.getpixel((24, 16)), RED)
        assert _close_to(jpeg.getpixel((24, 48)), BLUE)
        assert _close_to(jpeg.getpixel((2, 60)), BLUE)


def test_icc_profile_is_embedded(tmp_path: Path):
    output = tmp_path / "preview.jpg"
    _preview(_copy(ORIENTED_P3_HEIC, tmp_path), output)
    with Image.open(output) as jpeg, Image.open(S23_PHOTO) as p3:
        assert jpeg.info["icc_profile"] == p3.info["icc_profile"]


def test_heic_without_icc_profile_gives_a_jpeg_without_one(tmp_path: Path):
    output = tmp_path / "preview.jpg"
    result = _preview(_copy(PLAIN_HEIC, tmp_path), output)
    assert (result["width"], result["height"]) == (64, 32)
    with Image.open(output) as jpeg:
        assert "icc_profile" not in jpeg.info
        assert _close_to(jpeg.getpixel((2, 2)), GREEN)
        assert _close_to(jpeg.getpixel((16, 24)), RED)
        assert _close_to(jpeg.getpixel((48, 16)), BLUE)


def test_heic_with_alpha_is_flattened(tmp_path: Path):
    heic = tmp_path / "alpha.heic"
    pillow_heif.from_pillow(Image.new("RGBA", (16, 16), (*RED, 128))).save(heic)
    output = tmp_path / "preview.jpg"
    _preview(heic, output)
    with Image.open(output) as jpeg:
        assert jpeg.mode == "RGB"


def test_heic_named_like_a_jpeg_is_converted(tmp_path: Path):
    path = tmp_path / "photo.jpg"
    path.write_bytes(ORIENTED_P3_HEIC.read_bytes())
    assert _preview(path, tmp_path / "preview.jpg")["width"] == 32


def test_original_heic_is_unchanged(tmp_path: Path):
    heic = _copy(ORIENTED_P3_HEIC, tmp_path)
    _preview(heic, tmp_path / "preview.jpg")
    assert heic.read_bytes() == ORIENTED_P3_HEIC.read_bytes()
    assert heic.stat().st_mtime_ns == MTIME_NS


def test_output_folder_is_created(tmp_path: Path):
    output = tmp_path / "previews" / "photo.jpg"
    _preview(_copy(ORIENTED_P3_HEIC, tmp_path), output)
    assert output.is_file()


def test_existing_output_is_replaced_with_overwrite(tmp_path: Path):
    output = tmp_path / "preview.jpg"
    output.write_bytes(b"old")
    result = _preview(_copy(ORIENTED_P3_HEIC, tmp_path), output, "--overwrite")
    assert output.stat().st_size == result["size_bytes"] > 3


# --- Errors ---------------------------------------------------------------------


def test_jpeg_is_unsupported_format(tmp_path: Path):
    output = tmp_path / "preview.jpg"
    error = _assert_error(_invoke(_copy(S23_PHOTO, tmp_path), output), "unsupported_format", 21)
    assert error["details"] == {"detected_format": "jpeg"}
    assert not output.exists()


def test_existing_output_is_output_exists(tmp_path: Path):
    output = tmp_path / "preview.jpg"
    output.write_bytes(b"old")
    _assert_error(_invoke(_copy(ORIENTED_P3_HEIC, tmp_path), output), "output_exists", 24)
    assert output.read_bytes() == b"old"


def test_output_directory_is_output_exists(tmp_path: Path):
    _assert_error(_invoke(_copy(ORIENTED_P3_HEIC, tmp_path), tmp_path, "--overwrite"), "output_exists", 24)


def test_missing_input_is_input_not_found(tmp_path: Path):
    _assert_error(_invoke(tmp_path / "missing.heic", tmp_path / "preview.jpg"), "input_not_found", 23)


@pytest.mark.parametrize(
    ("kept", "decoder_message"),
    [(0.5, "Cannot read full meta box"), (0.9, "references data outside of file bounds")],
)
def test_truncated_heic_is_input_corrupt(tmp_path: Path, kept: float, decoder_message: str):
    data = ORIENTED_P3_HEIC.read_bytes()
    heic = tmp_path / "truncated.heic"
    heic.write_bytes(data[: int(len(data) * kept)])
    output = tmp_path / "preview.jpg"
    error = _assert_error(_invoke(heic, output), "input_corrupt", 27)
    assert error["details"] == {"detected_format": "heic"}
    assert decoder_message in error["message"]
    assert not output.exists()
    assert [path.name for path in tmp_path.iterdir()] == ["truncated.heic"]  # no temporary file left either


def test_tiled_heic_is_converted_whole(tmp_path: Path):
    output = tmp_path / "preview.jpg"
    result = _preview(_copy(TILED_HEIC, tmp_path), output)
    assert (result["width"], result["height"]) == (1024, 768)


def test_heic_cut_among_its_tiles_is_input_corrupt(tmp_path: Path):
    # libheif would fill the missing tiles in, and the preview would show them green
    data = TILED_HEIC.read_bytes()
    heic = tmp_path / "truncated.heic"
    heic.write_bytes(data[: len(data) // 2])
    output = tmp_path / "preview.jpg"
    error = _assert_error(_invoke(heic, output), "input_corrupt", 27)
    assert error["details"] == {"detected_format": "heic"}
    assert error["message"] == (
        f"Damaged heic file: file cut short, its image data runs to byte {len(data)} but the file has {len(data) // 2}"
    )
    assert not output.exists()
    assert [path.name for path in tmp_path.iterdir()] == ["truncated.heic"]


def test_heic_cut_only_in_its_trailing_thumbnail_is_input_corrupt(tmp_path: Path):
    data = TILED_HEIC.read_bytes()
    cut = data[:-100]
    # The thumbnail is stored last: the main image still decodes whole
    assert pillow_heif.open_heif(cut).to_pillow().tobytes() == pillow_heif.open_heif(data).to_pillow().tobytes()
    heic = tmp_path / "truncated.heic"
    heic.write_bytes(cut)
    output = tmp_path / "preview.jpg"
    error = _assert_error(_invoke(heic, output), "input_corrupt", 27)
    assert error["message"] == (
        f"Damaged heic file: file cut short, its image data runs to byte {len(data)} but the file has {len(cut)}"
    )
    assert not output.exists()


def test_heic_beyond_libheif_security_limits_is_input_corrupt(tmp_path: Path):
    output = tmp_path / "preview.jpg"
    error = _assert_error(_invoke(_copy(OVER_LIMIT_HEIC, tmp_path), output), "input_corrupt", 27)
    assert error["details"] == {"detected_format": "heic"}
    assert "Number of extents in iloc box (33) exceeds security limit (32)" in error["message"]
    assert not output.exists()


def test_libheif_out_of_memory_on_a_sound_heic_is_internal_error_not_corrupt(tmp_path: Path):
    output = tmp_path / "preview.jpg"
    with patch("pillow_heif.open_heif", side_effect=RuntimeError("Memory allocation error: Cannot allocate memory")):
        error = _assert_error(_invoke(_copy(TILED_HEIC, tmp_path), output), "internal_error", 50)
    assert "Memory allocation error" in error["message"]
    assert not output.exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any file")
def test_unreadable_input_is_input_unreadable(tmp_path: Path):
    heic = _copy(ORIENTED_P3_HEIC, tmp_path)
    heic.chmod(0)
    _assert_error(_invoke(heic, tmp_path / "preview.jpg"), "input_unreadable", 26)


def test_missing_output_option_is_a_json_usage_error(tmp_path: Path):
    result = runner.invoke(app, ["preview", str(_copy(ORIENTED_P3_HEIC, tmp_path)), "--json"])
    _assert_error(result, "usage_error", 2)


# --- Human-readable output ------------------------------------------------------


def test_without_json_reports_on_stderr(tmp_path: Path):
    output = tmp_path / "preview.jpg"
    result = runner.invoke(app, ["preview", str(_copy(ORIENTED_P3_HEIC, tmp_path)), "-o", str(output)])
    assert result.exit_code == 0, result.output
    assert result.stdout == ""
    assert "32x64" in result.stderr
    assert output.is_file()


def test_without_json_errors_go_to_stderr(tmp_path: Path):
    result = runner.invoke(app, ["preview", str(_copy(S23_PHOTO, tmp_path)), "-o", str(tmp_path / "preview.jpg")])
    assert result.exit_code == 21
    assert "Unsupported format: jpeg" in result.stderr
