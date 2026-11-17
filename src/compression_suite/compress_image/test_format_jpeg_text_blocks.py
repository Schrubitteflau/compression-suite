"""CLI tests for the PNG text blocks of compress-image --format jpeg - Tinify is mocked and returns the bytes it gets,
exiftool and Pillow run for real.

A text block with a standard equivalent in a JPEG is rewritten to it, so that photo managers read it; any other is
lost, and said so. No date is ever taken from the file's mtime.
"""

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image, PngImagePlugin
from typer.testing import CliRunner

from compression_suite.cli import app

runner = CliRunner()

TEXT_BLOCKS = Path(__file__).resolve().parent / "samples" / "text_blocks.png"

# Each text block of the sample, its value, and the JPEG tag it is rewritten to
MAPPED = {
    "PNG:Title": ("Sample title", "XMP-dc:Title"),
    "PNG:Description": ("Été au bord de l'eau", "XMP-dc:Description"),
    "PNG:Author": ("Test Author", "XMP-dc:Creator"),
    "PNG:Copyright": ("CC0 1.0", "XMP-dc:Rights"),
    "PNG:Comment": ("Fabricated for the compression-suite tests", "File:Comment"),
    "PNG:Software": ("make_png_samples.py", "IFD0:Software"),
    "PNG:CreationTime": ("2025:03:04 10:20:30", "XMP-xmp:CreateDate"),
}


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


def _convert(tmp_path: Path, src: Path = TEXT_BLOCKS, *args: str) -> dict:
    """Run compress-image --format jpeg --json on `src` and return the metadata diff."""
    with _echo_tinify():
        result = runner.invoke(
            app,
            ["compress-image", str(src), "--format", "jpeg", "--output", str(tmp_path / "out.jpg"), "--json", *args]
            + ["--api-key", "test-key"],
        )
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)["result"]["metadata"]


def test_each_mapped_text_block_is_in_the_jpeg_under_its_target_tag(tmp_path: Path):
    after = _convert(tmp_path)["after"]
    for value, target in MAPPED.values():
        assert after[target] == value, target


def test_each_mapped_text_block_is_an_expected_loss_naming_its_added_target(tmp_path: Path):
    metadata = _convert(tmp_path)
    expected = {loss["tag"]: loss for loss in metadata["expected_losses"]}
    added = {tag["tag"]: tag["after"] for tag in metadata["added"]}
    for block, (value, target) in MAPPED.items():
        assert expected[block]["before"] == value
        assert expected[block]["reason"] == f"bloc texte PNG réécrit en {target}"
        assert added[target] == value


def test_exiftool_does_not_stamp_the_xmp_it_writes(tmp_path: Path):
    assert "XMP-x:XMPToolkit" not in _convert(tmp_path)["after"]


def test_text_blocks_without_equivalent_are_the_only_unexpected_losses(tmp_path: Path):
    unexpected = _convert(tmp_path)["unexpected_losses"]
    assert {(loss["tag"], loss["kind"], loss["before"]) for loss in unexpected} == {
        ("PNG:Mood", "removed", "calm"),
        ("PNG:Disclaimer-fr", "removed", "Image fabriquée"),
    }


def test_creation_time_gives_the_same_capture_date(tmp_path: Path):
    important = {field["key"]: field for field in _convert(tmp_path)["important"]}
    assert important["capture_date"]["status"] == "same"
    assert important["capture_date"]["before"] == "2025:03:04 10:20:30"


def test_strip_rewrites_no_text_block(tmp_path: Path):
    metadata = _convert(tmp_path, TEXT_BLOCKS, "--metadata", "strip")
    assert metadata["unexpected_losses"] == []
    assert {loss["reason"] for loss in metadata["expected_losses"] if loss["tag"] in MAPPED} == {
        "métadonnées retirées à la demande"
    }
    assert not set(metadata["after"]) & {target for _, target in MAPPED.values()}


def _png(path: Path, xmp_title: str | None = None, **text_blocks: str) -> Path:
    """Write a small opaque PNG with these text blocks, and an XMP packet holding `xmp_title` if given."""
    info = PngImagePlugin.PngInfo()
    if xmp_title is not None:
        info.add_itxt(
            "XML:com.adobe.xmp",
            '<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
            '<rdf:Description rdf:about="" xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title><rdf:Alt>'
            f'<rdf:li xml:lang="x-default">{xmp_title}</rdf:li></rdf:Alt></dc:title></rdf:Description></rdf:RDF>'
            "</x:xmpmeta>",
        )
    for keyword, value in text_blocks.items():
        info.add_text(keyword.replace("_", " "), value)
    Image.new("RGB", (32, 24), (30, 120, 200)).save(path, pnginfo=info)
    return path


def test_a_creation_time_that_is_no_date_is_an_unexpected_loss(tmp_path: Path):
    metadata = _convert(tmp_path, _png(tmp_path / "in.png", Creation_Time="hier soir"))
    assert [(loss["tag"], loss["before"]) for loss in metadata["unexpected_losses"]] == [
        ("PNG:CreationTime", "hier soir")
    ]
    assert "XMP-xmp:CreateDate" not in metadata["after"]


def test_a_png_without_date_gives_a_jpeg_without_date_whatever_its_mtime(tmp_path: Path):
    src = _png(tmp_path / "in.png", Software="gnome-screenshot")
    old = 1_300_000_000_000_000_000  # 2011
    os.utime(src, ns=(old, old))
    metadata = _convert(tmp_path, src)
    assert not [tag for tag in metadata["after"] if "Date" in tag or "Time" in tag]
    [capture_date] = [field for field in metadata["important"] if field["key"] == "capture_date"]
    assert capture_date["source"] == "file_mtime"  # only the file's own date, kept by the output


@pytest.mark.parametrize(
    "creation_time",
    [
        "lun. 03 févr. 2025 09:05:01 CET",  # gnome-screenshot's %c in a French desktop
        "Tue Nov 17 14:56:58 2026",  # the same in an English one
        "04/03/2025 10:20",
    ],
)
def test_a_creation_time_in_free_text_writes_no_date(tmp_path: Path, creation_time: str):
    metadata = _convert(tmp_path, _png(tmp_path / "in.png", Creation_Time=creation_time))
    assert [(loss["tag"], loss["before"]) for loss in metadata["unexpected_losses"]] == [
        ("PNG:CreationTime", creation_time)
    ]
    assert not [tag for tag in metadata["after"] if "Date" in tag or "Time" in tag]


@pytest.mark.parametrize(
    ("creation_time", "written"),
    [
        ("2025-03-04T10:20:30+01:00", "2025:03:04 10:20:30+01:00"),  # ISO 8601
        ("Tue, 04 Mar 2025 10:20:30 GMT", "2025:03:04 10:20:30+00:00"),  # RFC 1123, which the PNG spec recommends
    ],
)
def test_a_creation_time_in_a_standard_format_is_the_same_capture_date(
    tmp_path: Path, creation_time: str, written: str
):
    metadata = _convert(tmp_path, _png(tmp_path / "in.png", Creation_Time=creation_time))
    assert metadata["unexpected_losses"] == []
    assert metadata["after"]["XMP-xmp:CreateDate"] == written
    [capture_date] = [field for field in metadata["important"] if field["key"] == "capture_date"]
    assert capture_date["status"] == "same"


def test_a_text_block_never_overwrites_the_tag_the_png_already_holds(tmp_path: Path):
    metadata = _convert(tmp_path, _png(tmp_path / "in.png", xmp_title="Titre XMP", Title="Titre PNG"))
    assert metadata["after"]["XMP-dc:Title"] == "Titre XMP"
    assert [(loss["tag"], loss["before"]) for loss in metadata["unexpected_losses"]] == [("PNG:Title", "Titre PNG")]


def test_a_text_block_equal_to_the_tag_the_png_already_holds_is_rewritten(tmp_path: Path):
    metadata = _convert(tmp_path, _png(tmp_path / "in.png", xmp_title="Même titre", Title="Même titre"))
    assert metadata["unexpected_losses"] == []
    reasons = {loss["tag"]: loss["reason"] for loss in metadata["expected_losses"]}
    assert reasons["PNG:Title"] == "bloc texte PNG réécrit en XMP-dc:Title"


def test_a_text_block_whose_value_does_not_fit_is_an_unexpected_loss(tmp_path: Path):
    comment = "x" * 70_000  # a JPEG comment segment holds at most 65 533 bytes
    metadata = _convert(tmp_path, _png(tmp_path / "in.png", Comment=comment))
    assert [loss["tag"] for loss in metadata["unexpected_losses"]] == ["PNG:Comment"]
