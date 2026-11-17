"""CLI tests for compress-image --format jpeg - Tinify is mocked and returns the bytes it gets, exiftool and Pillow run
for real.

Tinify echoing its input, the written file is the JPEG converted locally, with the original's metadata copied in.
"""

import io
import json
import os
import struct
import zlib
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image
from typer.testing import CliRunner

from compression_suite.cli import app
from compression_suite.utils.pinned_tools import expected_executable, install_dir, load_manifest, tools_dir

runner = CliRunner()

SAMPLES = Path(__file__).resolve().parent / "samples"
OPAQUE_PHOTO = SAMPLES / "opaque_photo.png"
PHOTO_JPEG = SAMPLES / "photo.jpg"
OPAQUE_RGBA = SAMPLES / "opaque_rgba.png"
TRANSPARENT_PIXEL = SAMPLES / "transparent_pixel.png"
TRUNCATED = SAMPLES / "truncated.png"


def _echo_tinify():
    """Patch tinify so the compressed result is the input itself."""
    mock = MagicMock()
    mock.compression_count = 42

    def make_source(data: bytes):
        source = MagicMock()
        source.to_buffer.return_value = data
        return source

    mock.from_buffer.side_effect = make_source
    return patch("compression_suite.compress_image.main.tinify", mock)


def _invoke(*args: str, input: bytes | None = None):
    return runner.invoke(app, ["compress-image", *args, "--api-key", "test-key"], input=input)


def _quantization_at(quality: int) -> dict:
    """The quantization tables Pillow writes at `quality`, the reference a JPEG's are compared to."""
    buf = io.BytesIO()
    Image.new("RGB", (16, 16)).save(buf, format="JPEG", quality=quality)
    with Image.open(buf) as image:
        return image.quantization  # ty: ignore[unresolved-attribute]


def test_opaque_png_becomes_a_420_jpeg_of_quality_92_compressed_once(tmp_path: Path):
    out = tmp_path / "photo.jpg"
    with _echo_tinify() as fake:
        result = _invoke(str(OPAQUE_PHOTO), "--format", "jpeg", "--output", str(out), "--json")

    assert result.exit_code == 0, result.output
    fake.from_buffer.assert_called_once()
    [sent] = fake.from_buffer.call_args.args
    assert sent[:3] == b"\xff\xd8\xff"
    with Image.open(out) as image:
        assert image.format == "JPEG"
        assert image.quantization == _quantization_at(92)  # ty: ignore[unresolved-attribute]
    after = json.loads(result.stdout)["result"]["metadata"]["after"]
    assert after["File:YCbCrSubSampling"] == "YCbCr4:2:0 (2 2)"


def _convert(tmp_path: Path, *args: str, src: Path = OPAQUE_PHOTO) -> dict:
    """Run compress-image --format jpeg --json on `src` and return the document."""
    with _echo_tinify():
        result = _invoke(str(src), "--format", "jpeg", "--output", str(tmp_path / "photo.jpg"), "--json", *args)
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


def test_result_tells_a_png_input_from_a_jpeg_output(tmp_path: Path):
    doc = _convert(tmp_path)
    assert doc["schema_version"] == 2
    assert doc["result"]["input"]["format"] == "png"
    assert doc["result"]["output"]["format"] == "jpeg"


def test_exif_xmp_and_icc_profile_of_the_png_are_in_the_jpeg(tmp_path: Path):
    after = _convert(tmp_path)["result"]["metadata"]["after"]
    assert after["IFD0:Make"] == "TestCamera"
    assert after["IFD0:Model"] == "TestModel"
    assert after["ExifIFD:DateTimeOriginal"] == "2025:03:04 10:20:30"
    assert after["GPS:GPSLatitude"] == "12 deg 20' 44.16\""
    assert after["XMP-dc:Subject"] == "sample"
    assert after["XMP-xmp:Rating"] == 4
    assert after["ICC_Profile:ProfileDescription"] == "DCI-P3 D65 Gamut with sRGB Transfer"


def test_every_important_field_is_the_same_transparency_included(tmp_path: Path):
    metadata = _convert(tmp_path)["result"]["metadata"]
    assert {field["key"]: field["status"] for field in metadata["important"]} == {
        "capture_date": "same",
        "gps": "same",
        "camera": "same",
        "dimensions": "same",
        "orientation": "same",
        "color_profile": "same",
        "transparency": "same",
    }


def test_png_encoding_fields_are_expected_losses_of_the_conversion(tmp_path: Path):
    metadata = _convert(tmp_path)["result"]["metadata"]
    assert metadata["unexpected_losses"] == []
    reasons = {loss["tag"]: loss["reason"] for loss in metadata["expected_losses"]}
    for tag in ("PNG:BitDepth", "PNG:ColorType", "PNG:Compression", "PNG:Filter", "PNG:Interlace"):
        assert reasons[tag] == "format converti en JPEG"


def test_strip_drops_exif_and_xmp_with_every_loss_expected(tmp_path: Path):
    metadata = _convert(tmp_path, "--metadata", "strip")["result"]["metadata"]
    assert not any(tag.startswith(("IFD0:", "ExifIFD:", "GPS:", "XMP")) for tag in metadata["after"])
    assert metadata["unexpected_losses"] == []
    reasons = {loss["tag"]: loss["reason"] for loss in metadata["expected_losses"]}
    assert reasons["IFD0:Make"] == "métadonnées retirées à la demande"
    assert reasons["PNG:ColorType"] == "format converti en JPEG"


# --- Input triage ---------------------------------------------------------------


def _error(result) -> dict:
    doc = json.loads(result.stdout)
    assert doc["ok"] is False
    return doc["error"]


def test_jpeg_input_is_a_plain_compression(tmp_path: Path):
    out = tmp_path / "photo.jpg"
    with _echo_tinify() as fake:
        result = _invoke(str(PHOTO_JPEG), "--format", "jpeg", "--output", str(out), "--json")
    assert result.exit_code == 0, result.output
    fake.from_buffer.assert_called_once_with(PHOTO_JPEG.read_bytes())
    metadata = json.loads(result.stdout)["result"]["metadata"]
    assert metadata["unexpected_losses"] == []
    assert not any(loss["reason"] == "format converti en JPEG" for loss in metadata["expected_losses"])


@pytest.mark.parametrize(("fmt", "detected"), [("WEBP", "webp"), ("AVIF", "avif"), ("GIF", "gif")])
def test_other_formats_are_unsupported_before_tinify(tmp_path: Path, fmt: str, detected: str):
    src = tmp_path / "image.bin"
    Image.new("RGB", (32, 24), (200, 30, 30)).save(src, format=fmt)
    out = tmp_path / "out.jpg"
    with _echo_tinify() as fake:
        result = _invoke(str(src), "--format", "jpeg", "--output", str(out), "--json")
    assert result.exit_code == 21, result.output
    error = _error(result)
    assert error["code"] == "unsupported_format"
    assert error["details"] == {"detected_format": detected}
    assert not fake.from_buffer.called
    assert not out.exists()


def test_other_format_values_are_a_usage_error(tmp_path: Path):
    with _echo_tinify() as fake:
        result = _invoke(str(OPAQUE_PHOTO), "--format", "webp", "--output", str(tmp_path / "out"), "--json")
    assert result.exit_code == 2, result.output
    error = _error(result)
    assert error["code"] == "usage_error"
    assert "'jpeg'" in error["message"]  # the one value --format takes
    assert not fake.from_buffer.called


# --- Input and output ---------------------------------------------------------


def test_stdin_to_stdout(tmp_path: Path):
    with _echo_tinify():
        result = _invoke("--format", "jpeg", input=OPAQUE_PHOTO.read_bytes())
    assert result.exit_code == 0, result.stderr
    with Image.open(io.BytesIO(result.stdout_bytes)) as image:
        assert image.format == "JPEG"
        assert image.getexif()[0x010F] == "TestCamera"  # Make, copied from the PNG


def test_stdin_with_json_reports_the_conversion(tmp_path: Path):
    with _echo_tinify():
        result = _invoke(
            "--format", "jpeg", "--output", str(tmp_path / "photo.jpg"), "--json", input=OPAQUE_PHOTO.read_bytes()
        )
    assert result.exit_code == 0, result.output
    res = json.loads(result.stdout)["result"]
    assert (res["input"]["format"], res["output"]["format"]) == ("png", "jpeg")
    assert res["metadata"]["unexpected_losses"] == []


def test_output_is_written_atomically_with_the_original_dates_whatever_its_extension(tmp_path: Path):
    src = tmp_path / "photo.png"
    src.write_bytes(OPAQUE_PHOTO.read_bytes())
    mtime_ns = 1_660_039_872_654_321_000
    os.utime(src, ns=(mtime_ns, mtime_ns))
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    out = out_dir / "photo.png"
    with _echo_tinify():
        result = _invoke(str(src), "--format", "jpeg", "--output", str(out), "--json")
    assert result.exit_code == 0, result.output
    assert sorted(p.name for p in out_dir.iterdir()) == ["photo.png"]
    assert out.read_bytes()[:3] == b"\xff\xd8\xff"
    assert out.stat().st_mtime_ns == mtime_ns
    res = json.loads(result.stdout)["result"]
    assert res["output"]["path"] == str(out)
    assert res["output"]["mtime"] == res["input"]["mtime"]


def test_without_format_a_png_stays_a_png(tmp_path: Path):
    out = tmp_path / "photo.png"
    with _echo_tinify() as fake:
        result = _invoke(str(OPAQUE_PHOTO), "--output", str(out), "--json")
    assert result.exit_code == 0, result.output
    fake.from_buffer.assert_called_once_with(OPAQUE_PHOTO.read_bytes())
    assert json.loads(result.stdout)["result"]["output"]["format"] == "png"


# --- Transparency and damaged PNGs ----------------------------------------------


def test_a_png_with_one_transparent_pixel_is_refused_before_tinify(tmp_path: Path):
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    with _echo_tinify() as fake:
        result = _invoke(str(TRANSPARENT_PIXEL), "--format", "jpeg", "--output", str(out_dir / "out.jpg"), "--json")
    assert result.exit_code == 28, result.output
    assert _error(result)["code"] == "transparent_input"
    assert not fake.from_buffer.called
    assert list(out_dir.iterdir()) == []


GRAY, RGB = 0, 2  # PNG colour types


def _png16_with_key(path: Path, colour_type: int, sample: tuple[int, ...], key: tuple[int, ...]) -> Path:
    """Write a 16-bit PNG whose every pixel holds `sample`, with a tRNS colour key `key`, by hand: Pillow can't write
    a 16-bit RGB PNG."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    width, height = 64, 48
    row = b"\0" + struct.pack(f">{len(sample)}H", *sample) * width  # filter type 0
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 16, colour_type, 0, 0, 0))
        + chunk(b"tRNS", struct.pack(f">{len(key)}H", *key))
        + chunk(b"IDAT", zlib.compress(row * height))
        + chunk(b"IEND", b"")
    )
    return path


@pytest.mark.parametrize(
    ("colour_type", "sample"),
    [(GRAY, (30000,)), (RGB, (1000, 2000, 3000))],
    ids=["gray16", "rgb16"],
)
def test_a_16_bit_png_whose_colour_key_matches_its_pixels_is_refused_before_tinify(
    tmp_path: Path, colour_type: int, sample: tuple[int, ...]
):
    src = _png16_with_key(tmp_path / "in.png", colour_type, sample, key=sample)
    with _echo_tinify() as fake:
        result = _invoke(str(src), "--format", "jpeg", "--output", str(tmp_path / "out.jpg"), "--json")
    assert result.exit_code == 28, result.output
    assert _error(result)["code"] == "transparent_input"
    assert not fake.from_buffer.called


@pytest.mark.parametrize(
    ("colour_type", "sample", "key"),
    [
        (GRAY, (30000,), (30001,)),
        # Same 8-bit pixels: the key differs only in the high byte of red, which Pillow's 8-bit reading drops
        (RGB, (0x0A0A, 0x1414, 0x8080), (0x0B0A, 0x1414, 0x8080)),
    ],
    ids=["gray16", "rgb16"],
)
def test_a_16_bit_png_whose_colour_key_matches_no_pixel_is_converted(
    tmp_path: Path, colour_type: int, sample: tuple[int, ...], key: tuple[int, ...]
):
    src = _png16_with_key(tmp_path / "in.png", colour_type, sample, key)
    metadata = _convert(tmp_path, src=src)["result"]["metadata"]
    [transparency] = [field for field in metadata["important"] if field["key"] == "transparency"]
    assert (transparency["before"], transparency["status"]) == (False, "same")


def _tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ffmpeg: str | None, version: str = "") -> None:
    """Use a tools folder holding the real exiftool and, given its script, an ffmpeg of the pinned version, which runs
    `version` before giving it."""
    exiftool, ffmpeg_tool = load_manifest()["exiftool"], load_manifest()["ffmpeg"]
    tools = tmp_path / "tools"
    tools.mkdir()
    real_exiftool = install_dir(exiftool, tools_dir())
    (tools / real_exiftool.name).symlink_to(real_exiftool)
    if ffmpeg is not None:
        executable = tools / install_dir(ffmpeg_tool, tools_dir()).name / ffmpeg_tool.executable
        executable.parent.mkdir(parents=True)
        executable.write_text(
            f'#!/bin/sh\nif [ "$1" = -version ]; then\n{version}\n'
            f'echo "ffmpeg version {ffmpeg_tool.version}"\nexit 0\nfi\n{ffmpeg}\n'
        )
        executable.chmod(0o755)
    monkeypatch.setenv("COMPRESSION_SUITE_TOOLS_DIR", str(tools))


def test_a_png_whose_transparency_ffmpeg_can_t_tell_is_refused_before_tinify(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    src = _png16_with_key(tmp_path / "in.png", RGB, (1000, 2000, 3000), key=(1000, 2000, 3000))
    _tools(tmp_path, monkeypatch, ffmpeg="echo boom >&2; exit 1")
    with _echo_tinify() as fake:
        result = _invoke(str(src), "--format", "jpeg", "--output", str(tmp_path / "out.jpg"), "--json")
    assert result.exit_code == 28, result.output
    assert _error(result)["code"] == "transparent_input"
    assert not fake.from_buffer.called


def test_without_format_a_missing_ffmpeg_fails_before_tinify(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    src = _png16_with_key(tmp_path / "in.png", RGB, (1000, 2000, 3000), key=(1000, 2000, 3000))
    _tools(tmp_path, monkeypatch, ffmpeg=None)
    with _echo_tinify() as fake:
        result = _invoke(str(src), "--output", str(tmp_path / "out.png"), "--json")
    assert result.exit_code == 30, result.output
    assert _error(result)["details"]["tool"] == "ffmpeg"
    assert not fake.from_buffer.called


def test_an_output_whose_transparency_can_t_be_read_after_tinify_is_unverified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    real_ffmpeg = expected_executable(load_manifest()["ffmpeg"])
    checked = tmp_path / "version-checked"
    # The real ffmpeg, whose version check passes for the original and fails for the output, as if it went missing
    _tools(
        tmp_path,
        monkeypatch,
        ffmpeg=f'exec "{real_ffmpeg}" "$@"',
        version=f'[ -e "{checked}" ] && exit 1; touch "{checked}"',
    )
    src = _png16_with_key(tmp_path / "in.png", RGB, (1000, 2000, 3000), key=(1000, 2000, 3000))
    with _echo_tinify():
        result = _invoke(str(src), "--output", str(tmp_path / "out.png"), "--json")
    assert result.exit_code == 0, result.output
    important = {field["key"]: field for field in json.loads(result.stdout)["result"]["metadata"]["important"]}
    assert (important["transparency"]["before"], important["transparency"]["status"]) == (True, "unverified")


def test_a_rgba_png_whose_pixels_are_all_opaque_becomes_an_rgb_jpeg(tmp_path: Path):
    out = tmp_path / "photo.jpg"
    with _echo_tinify() as fake:
        result = _invoke(str(OPAQUE_RGBA), "--format", "jpeg", "--output", str(out), "--json")
    assert result.exit_code == 0, result.output
    fake.from_buffer.assert_called_once()
    with Image.open(out) as image:
        assert (image.format, image.mode) == ("JPEG", "RGB")
    transparency = {field["key"]: field for field in json.loads(result.stdout)["result"]["metadata"]["important"]}
    assert transparency["transparency"]["status"] == "same"


def test_a_truncated_png_is_input_corrupt_before_tinify(tmp_path: Path):
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    with _echo_tinify() as fake:
        result = _invoke(str(TRUNCATED), "--format", "jpeg", "--output", str(out_dir / "out.jpg"), "--json")
    assert result.exit_code == 27, result.output
    assert _error(result)["code"] == "input_corrupt"
    assert not fake.from_buffer.called
    assert list(out_dir.iterdir()) == []


def test_a_transparent_png_from_stdin_is_refused_too():
    with _echo_tinify() as fake:
        result = _invoke("--format", "jpeg", input=TRANSPARENT_PIXEL.read_bytes())
    assert result.exit_code == 28
    assert not fake.from_buffer.called
    assert result.stdout_bytes == b""


def test_without_format_a_transparent_png_is_compressed_as_a_png_keeping_its_transparency(tmp_path: Path):
    out = tmp_path / "out.png"
    with _echo_tinify() as fake:
        result = _invoke(str(TRANSPARENT_PIXEL), "--output", str(out), "--json")
    assert result.exit_code == 0, result.output
    fake.from_buffer.assert_called_once_with(TRANSPARENT_PIXEL.read_bytes())
    res = json.loads(result.stdout)["result"]
    assert res["output"]["format"] == "png"
    important = {field["key"]: field for field in res["metadata"]["important"]}
    assert (important["transparency"]["before"], important["transparency"]["status"]) == (True, "same")


# --- Pixel modes ----------------------------------------------------------------
# The mode samples are 256-column gradients: column `x` holds the 8-bit level `x`, `x * 257` on 16 bits
# (make_png_samples.py).

GRADIENT_ROW = 8


def _converted_row(tmp_path: Path, name: str) -> tuple[str, list]:
    """Convert the mode sample `name` and return the output's mode and the pixels of its middle row."""
    out = tmp_path / f"{name}.jpg"
    with _echo_tinify():
        result = _invoke(str(SAMPLES / f"{name}.png"), "--format", "jpeg", "--output", str(out), "--json")
    assert result.exit_code == 0, result.output
    with Image.open(out) as image:
        return image.mode, [image.getpixel((x, GRADIENT_ROW)) for x in range(image.width)]


def _close(actual, expected, tolerance: int = 4) -> bool:
    return all(abs(a - e) <= tolerance for a, e in zip(actual, expected, strict=True))


@pytest.mark.parametrize("name", ["gray", "gray16", "gray16_alpha", "gray16_icc"])
def test_grayscale_png_becomes_a_one_component_jpeg_with_its_gradient(tmp_path: Path, name: str):
    mode, row = _converted_row(tmp_path, name)
    assert mode == "L"
    assert _close(row, range(256)), row


@pytest.mark.parametrize("name", ["rgb16", "palette"])
def test_16_bit_and_palette_png_become_an_rgb_jpeg_with_its_gradient(tmp_path: Path, name: str):
    mode, row = _converted_row(tmp_path, name)
    assert mode == "RGB"
    # A palette of 64 colours rounds each level by up to 4 more
    tolerance = 8 if name == "palette" else 4
    assert all(_close(pixel, (x, 255 - x, 128), tolerance) for x, pixel in enumerate(row)), row


@pytest.mark.parametrize(
    ("name", "encoding"),
    [
        ("gray", {"PNG:BitDepth": 8, "PNG:ColorType": "Grayscale"}),
        ("gray16", {"PNG:BitDepth": 16, "PNG:ColorType": "Grayscale"}),
        ("rgb16", {"PNG:BitDepth": 16, "PNG:ColorType": "RGB"}),
        ("palette", {"PNG:BitDepth": 8, "PNG:ColorType": "Palette", "PNG:Palette": None}),
        (
            "gray16_alpha",
            {"PNG:BitDepth": 16, "PNG:ColorType": "Grayscale with Alpha", "PNG:SignificantBits": "16 16"},
        ),
    ],
)
def test_bit_depth_colour_type_and_palette_are_expected_losses_of_the_conversion(
    tmp_path: Path, name: str, encoding: dict
):
    metadata = _convert(tmp_path, src=SAMPLES / f"{name}.png")["result"]["metadata"]
    assert metadata["unexpected_losses"] == []
    losses = {loss["tag"]: loss for loss in metadata["expected_losses"]}
    for tag, before in encoding.items():
        assert losses[tag]["reason"] == "format converti en JPEG"
        if before is not None:
            assert losses[tag]["before"] == before


def test_16_bit_gray_png_with_a_gray_profile_keeps_its_colours(tmp_path: Path):
    metadata = _convert(tmp_path, src=SAMPLES / "gray16_icc.png")["result"]["metadata"]
    assert metadata["unexpected_losses"] == []
