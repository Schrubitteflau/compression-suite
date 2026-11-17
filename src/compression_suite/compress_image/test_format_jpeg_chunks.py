"""CLI tests for the ancillary chunks of a typical opaque PNG under compress-image --format jpeg - Tinify is mocked and
returns the bytes it gets, exiftool and Pillow run for real.

A chunk only a PNG has a use for is an expected loss of the conversion; a date or a resolution is carried over to its
JPEG tag, and shown as moved there.
"""

import json
import struct
import zlib
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image, PngImagePlugin
from typer.testing import CliRunner

from compression_suite.cli import app

runner = CliRunner()

SAMPLES = Path(__file__).resolve().parent / "samples"
TYPICAL_CHUNKS = SAMPLES / "typical_chunks.png"
PLAIN = SAMPLES / "gray.png"  # no metadata

CONVERTED_REASON = "format converti en JPEG"

MAKE, MODIFY_DATE = 0x010F, 0x0132  # EXIF IFD0 tags
EXIF_IFD, OFFSET_TIME = 0x8769, 0x9010  # the Exif sub-IFD, and its tag
X_RESOLUTION, Y_RESOLUTION, RESOLUTION_UNIT, INCHES = 0x011A, 0x011B, 0x0128, 2  # EXIF IFD0 tags, and a unit

PHYS_TAGS = ("PNG-pHYs:PixelsPerUnitX", "PNG-pHYs:PixelsPerUnitY", "PNG-pHYs:PixelUnits")


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


def _convert(tmp_path: Path, src: Path = TYPICAL_CHUNKS, *args: str) -> dict:
    """Run compress-image --format jpeg --json on `src` and return the metadata diff."""
    with _echo_tinify():
        result = runner.invoke(
            app,
            ["compress-image", str(src), "--format", "jpeg", "--output", str(tmp_path / "out.jpg"), "--json", *args]
            + ["--api-key", "test-key"],
        )
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)["result"]["metadata"]


def _png_with_chunks(path: Path, *chunks: tuple[bytes, bytes], src: Path = PLAIN) -> Path:
    """Write `src` with `chunks` inserted right after its IHDR chunk."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    png = src.read_bytes()
    ihdr_end = 8 + 8 + 13 + 4  # signature, then the IHDR chunk's length and type, data and CRC
    path.write_bytes(png[:ihdr_end] + b"".join(chunk(kind, data) for kind, data in chunks) + png[ihdr_end:])
    return path


def _info(*chunks: tuple[bytes, bytes]) -> PngImagePlugin.PngInfo:
    """Chunks for Pillow to write into a PNG."""
    info = PngImagePlugin.PngInfo()
    for kind, data in chunks:
        info.add(kind, data)
    return info


def test_srgb_gamma_and_background_chunks_are_expected_losses_of_the_conversion(tmp_path: Path):
    reasons = {loss["tag"]: loss["reason"] for loss in _convert(tmp_path)["expected_losses"]}
    for tag in ("PNG:SRGBRendering", "PNG:Gamma", "PNG:BackgroundColor"):
        assert reasons[tag] == CONVERTED_REASON, tag


def test_a_gamma_other_than_srgb_s_is_an_unexpected_loss(tmp_path: Path):
    src = _png_with_chunks(tmp_path / "in.png", (b"gAMA", struct.pack(">I", 45454)))
    unexpected = _convert(tmp_path, src)["unexpected_losses"]
    assert [loss["tag"] for loss in unexpected] == ["PNG:Gamma"]


def test_the_modification_date_is_moved_to_the_exif_modify_date(tmp_path: Path):
    metadata = _convert(tmp_path)
    assert metadata["after"]["IFD0:ModifyDate"] == "2025:03:04 09:20:30"
    reasons = {loss["tag"]: loss["reason"] for loss in metadata["expected_losses"]}
    assert reasons["PNG:ModifyDate"] == "bloc PNG réécrit en IFD0:ModifyDate"
    assert {"tag": "IFD0:ModifyDate", "after": "2025:03:04 09:20:30"} in metadata["added"]


def test_the_modification_date_is_said_to_be_utc_as_the_png_s_always_is(tmp_path: Path):
    metadata = _convert(tmp_path)
    assert {"tag": "ExifIFD:OffsetTime", "after": "+00:00"} in metadata["added"]


def test_the_modification_date_never_overwrites_the_exif_one_of_the_png(tmp_path: Path):
    exif = Image.Exif()
    exif[MODIFY_DATE] = "2024:01:02 03:04:05"
    src = tmp_path / "in.png"
    Image.open(TYPICAL_CHUNKS).save(
        src, exif=exif, pnginfo=_info((b"tIME", struct.pack(">HBBBBB", 2025, 3, 4, 9, 20, 30)))
    )
    metadata = _convert(tmp_path, src)
    assert metadata["after"]["IFD0:ModifyDate"] == "2024:01:02 03:04:05"
    assert [(loss["tag"], loss["before"]) for loss in metadata["unexpected_losses"]] == [
        ("PNG:ModifyDate", "2025:03:04 09:20:30")
    ]


def test_the_resolution_in_pixels_per_metre_is_moved_to_the_exif_resolution_in_cm(tmp_path: Path):
    metadata = _convert(tmp_path)
    moved = {
        "PNG-pHYs:PixelsPerUnitX": ("IFD0:XResolution", 37.8),
        "PNG-pHYs:PixelsPerUnitY": ("IFD0:YResolution", 37.8),
        "PNG-pHYs:PixelUnits": ("IFD0:ResolutionUnit", "cm"),
    }
    reasons = {loss["tag"]: loss["reason"] for loss in metadata["expected_losses"]}
    added = {tag["tag"]: tag["after"] for tag in metadata["added"]}
    for tag, (target, value) in moved.items():
        assert reasons[tag] == f"bloc PNG réécrit en {target}"
        assert added[target] == value
    assert metadata["unexpected_losses"] == []


def test_an_aspect_ratio_without_unit_is_moved_to_the_jfif_one_not_to_an_exif_resolution(tmp_path: Path):
    src = _png_with_chunks(tmp_path / "in.png", (b"pHYs", struct.pack(">IIB", 3000, 2000, 0)))
    metadata = _convert(tmp_path, src)
    assert metadata["unexpected_losses"] == []
    assert (
        metadata["after"]["JFIF:XResolution"],
        metadata["after"]["JFIF:YResolution"],
        metadata["after"]["JFIF:ResolutionUnit"],
    ) == (3, 2, "None")
    reasons = {loss["tag"]: loss["reason"] for loss in metadata["expected_losses"]}
    assert reasons["PNG-pHYs:PixelsPerUnitX"] == "bloc PNG réécrit en JFIF:XResolution"
    assert not [tag for tag in metadata["after"] if tag.startswith("IFD0:")]


def test_the_modification_date_is_not_moved_when_the_png_holds_a_utc_offset(tmp_path: Path):
    exif = Image.Exif()
    exif[MAKE] = "TestCamera"  # Pillow writes no Exif sub-IFD under an empty IFD0
    exif.get_ifd(EXIF_IFD)[OFFSET_TIME] = "+01:00"
    src = tmp_path / "in.png"
    Image.open(TYPICAL_CHUNKS).save(
        src, exif=exif, pnginfo=_info((b"tIME", struct.pack(">HBBBBB", 2025, 3, 4, 9, 20, 30)))
    )
    metadata = _convert(tmp_path, src)
    assert "IFD0:ModifyDate" not in metadata["after"]
    assert metadata["after"]["ExifIFD:OffsetTime"] == "+01:00"
    assert [(loss["tag"], loss["before"]) for loss in metadata["unexpected_losses"]] == [
        ("PNG:ModifyDate", "2025:03:04 09:20:30")
    ]


@pytest.mark.parametrize("time", [(2025, 13, 4, 9, 20, 30), (0, 0, 0, 0, 0, 0)], ids=["month_13", "all_zero"])
def test_an_invalid_modification_date_writes_no_date_and_no_offset(tmp_path: Path, time: tuple[int, ...]):
    src = _png_with_chunks(tmp_path / "in.png", (b"tIME", struct.pack(">HBBBBB", *time)))
    metadata = _convert(tmp_path, src)
    assert [loss["tag"] for loss in metadata["unexpected_losses"]] == ["PNG:ModifyDate"]
    assert not [tag for tag in metadata["after"] if tag.startswith(("IFD0:", "ExifIFD:"))]


@pytest.mark.parametrize("pixels_per_metre", [10000, 2000, 3800])
def test_a_whole_resolution_in_cm_is_moved(tmp_path: Path, pixels_per_metre: int):
    src = _png_with_chunks(tmp_path / "in.png", (b"pHYs", struct.pack(">IIB", pixels_per_metre, pixels_per_metre, 1)))
    metadata = _convert(tmp_path, src)
    assert metadata["unexpected_losses"] == []
    assert metadata["after"]["IFD0:XResolution"] == pixels_per_metre / 100


def _png_with_exif_resolution(path: Path, dpi: float, pixels_per_metre: int) -> Path:
    """A PNG whose EXIF gives `dpi` in inches, with a pHYs chunk of `pixels_per_metre`, like a macOS screenshot."""
    exif = Image.Exif()
    exif[X_RESOLUTION] = exif[Y_RESOLUTION] = dpi
    exif[RESOLUTION_UNIT] = INCHES
    _png_with_chunks(path, (b"pHYs", struct.pack(">IIB", pixels_per_metre, pixels_per_metre, 1)))
    Image.open(path).save(path, exif=exif, dpi=(pixels_per_metre * 0.0254,) * 2)
    return path


def test_a_resolution_the_png_s_exif_already_gives_in_inches_is_moved(tmp_path: Path):
    src = _png_with_exif_resolution(tmp_path / "in.png", 144, 5669)
    metadata = _convert(tmp_path, src)
    assert metadata["unexpected_losses"] == []
    assert (metadata["after"]["IFD0:XResolution"], metadata["after"]["IFD0:ResolutionUnit"]) == (144, "inches")


def test_a_resolution_other_than_the_png_s_exif_one_is_lost_and_never_written_over_it(tmp_path: Path):
    src = _png_with_exif_resolution(tmp_path / "in.png", 72, 5669)
    metadata = _convert(tmp_path, src)
    assert {loss["tag"] for loss in metadata["unexpected_losses"]} == set(PHYS_TAGS)
    assert (metadata["after"]["IFD0:XResolution"], metadata["after"]["IFD0:ResolutionUnit"]) == (72, "inches")


@pytest.mark.parametrize(
    "phys",
    [(70001, 1, 0), (3, 70001, 0), (0, 0, 1), (3780, 1890, 2)],
    ids=["ratio_over_16_bits", "one_side_over_16_bits", "zero", "invalid_unit"],
)
def test_a_resolution_that_can_t_be_moved_is_lost_and_nothing_is_written(tmp_path: Path, phys: tuple[int, int, int]):
    src = _png_with_chunks(tmp_path / "in.png", (b"pHYs", struct.pack(">IIB", *phys)))
    metadata = _convert(tmp_path, src)
    assert {loss["tag"] for loss in metadata["unexpected_losses"]} == set(PHYS_TAGS)
    assert not [tag for tag in metadata["after"] if tag.startswith("IFD0:")]
    jfif = tuple(metadata["after"][f"JFIF:{name}"] for name in ("XResolution", "YResolution", "ResolutionUnit"))
    assert jfif == (1, 1, "None")  # the JFIF the conversion writes by default


def test_the_unit_is_moved_only_with_the_resolution(tmp_path: Path):
    src = _png_with_chunks(tmp_path / "in.png", (b"pHYs", struct.pack(">IIB", 0xFFFFFFFF, 0xFFFFFFFF, 1)))
    metadata = _convert(tmp_path, src)
    expected = {loss["tag"] for loss in metadata["expected_losses"]}
    unexpected = {loss["tag"] for loss in metadata["unexpected_losses"]}
    x, y, unit = PHYS_TAGS
    assert (unit in expected) == ({x, y} <= expected)
    assert {x, y, unit} <= expected | unexpected


@pytest.mark.parametrize("held", ["unit", "x"])
def test_no_exif_resolution_is_written_when_the_png_holds_part_of_it(tmp_path: Path, held: str):
    exif = Image.Exif()
    if held == "unit":
        exif[RESOLUTION_UNIT] = INCHES
    else:
        exif[X_RESOLUTION] = 72.0
    src = _png_with_chunks(tmp_path / "in.png", (b"pHYs", struct.pack(">IIB", 3780, 3780, 1)))
    Image.open(src).save(src, exif=exif, dpi=(3780 * 0.0254,) * 2)
    metadata = _convert(tmp_path, src)
    assert {loss["tag"] for loss in metadata["unexpected_losses"]} == set(PHYS_TAGS)
    assert metadata["after"].get("IFD0:XResolution") != 37.8
    assert metadata["after"].get("IFD0:ResolutionUnit") != "cm"
